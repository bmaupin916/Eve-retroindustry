import asyncio
import hashlib
import time
from collections import OrderedDict

import httpx

from typing import Optional

from app.version import USER_AGENT

ESI_BASE = "https://esi.evetech.net/latest"
FUZZWORK_BASE = "https://www.fuzzwork.co.uk"
ESI_HOST = "esi.evetech.net"

# ESI date-based versioning: pin behavior to a fixed date (X-Compatibility-Date)
# so future breaking changes don't break us. Change the date only on a deliberate switch
# to newer API behavior. /latest in the URL still works; the header takes precedence.
ESI_COMPAT_DATE = "2026-07-17"

# USER_AGENT is sent on every request this client makes — including the non-ESI
# hosts (GitHub, images), where it is harmless. See app/version.py for why CCP
# cares. This closes finding 7 of the design doc's security baseline.


# The connection pool must cover our concurrency (semaphores up to 30), otherwise refresh
# is the bottleneck: httpx's default max_keepalive_connections=20 recycles only ~20
# connections and the rest pay the TLS handshake over and over — with keepalive 50 the bulk
# volume/orders refresh is ~2.8x faster (measured). We stay at 30 concurrent
# (semaphore), so under the ESI rate limit.
_ESI_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=50)


class ESIErrorLimited(httpx.HTTPStatusError):
    """Raised (via raise_for_status) when ESI returns HTTP 420 — the whole
    client is error-limited and no request will succeed until the window resets."""


# --- ESI error-limit governor -------------------------------------------------
# ESI keeps a per-client error budget in a ~60s sliding window and returns HTTP
# 420 ("error limited") for EVERY request once it is exhausted — so a single
# innocent GET (e.g. Plan → assets) fails only because something else (Sync All
# across many characters, a price refresh) burned the budget. Every ESI response
# carries X-ESI-Error-Limit-Remain / -Reset; we watch them across ALL esi_client()
# instances (process-global state) and self-throttle before hitting the cliff,
# then hard-wait through the reset window if we do hit a 420.
class _ErrorLimitGovernor:
    def __init__(self) -> None:
        self._pause_until = 0.0  # loop time.monotonic() deadline; 0 = clear

    async def wait(self) -> None:
        # Block new ESI requests while a pause is in effect. Re-check in short
        # slices so a concurrently-updated deadline is honored promptly.
        while True:
            delay = self._pause_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 2.0))

    def observe(self, remain: Optional[int], reset: Optional[int]) -> None:
        # Proactively back off when the budget is nearly gone, so we stop
        # BEFORE the 420 cliff rather than after.
        if remain is not None and reset is not None and remain <= 5:
            self._pause_until = max(self._pause_until, time.monotonic() + reset + 1)

    def blocked(self, reset: Optional[int]) -> None:
        # Got a 420: freeze all ESI traffic until the window resets.
        self._pause_until = max(self._pause_until, time.monotonic() + (reset or 60) + 1)


_ERROR_LIMIT = _ErrorLimitGovernor()


# --- ESI token-bucket rate limiter (the newer, per-group one) -----------------
# Alongside the old error limit, ESI now meters *successful* traffic with a token
# bucket and answers 429 + Retry-After when a bucket runs dry. Two properties
# shape this code:
#
#   * The bucket is per RATE LIMIT GROUP, not per client. Pausing everything on a
#     429 — the way the error limit governor has to — would let a drained market
#     bucket stop unrelated wallet or asset calls. So state is kept per group.
#   * A request does not say which group it belongs to; only the response does,
#     via X-Ratelimit-Group. We therefore learn the mapping from responses, keyed
#     by the URL path with its numeric segments collapsed, so
#     /markets/10000002/orders/ and /markets/10000043/orders/ share one entry.
#
# Costs (per CCP's docs): 2xx = 2 tokens, 3xx = 1, 4xx = 5, 5xx = 0, and a 429
# itself is free — so being throttled does not dig the hole deeper.
class _TokenBucketGovernor:
    # Below this share of the bucket, pause the group briefly between bursts.
    # The real protection is the 429 path; this only keeps us off the cliff, as
    # CCP's "Best Practices" ask ("Don't operate at the limit").
    _LOW_WATER = 0.10
    _BRAKE_SECONDS = 2.0

    def __init__(self) -> None:
        self._pause_until: dict[str, float] = {}   # group -> monotonic deadline
        self._group_of: dict[str, str] = {}        # path signature -> group

    @staticmethod
    def signature(url: httpx.URL) -> str:
        """Path with numeric segments collapsed, so all regions share a bucket key."""
        parts = ("{}" if seg.isdigit() else seg for seg in url.path.split("/"))
        return "/".join(parts)

    async def wait(self, sig: str) -> None:
        group = self._group_of.get(sig)
        if group is None:
            return
        while True:
            delay = self._pause_until.get(group, 0.0) - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 2.0))

    def observe(self, sig: str, response: httpx.Response) -> None:
        group = response.headers.get("x-ratelimit-group")
        if not group:
            return
        self._group_of[sig] = group
        remaining = _int_header(response, "x-ratelimit-remaining")
        limit = _limit_header_total(response.headers.get("x-ratelimit-limit"))
        if remaining is not None and limit and remaining <= limit * self._LOW_WATER:
            self._pause_until[group] = max(
                self._pause_until.get(group, 0.0), time.monotonic() + self._BRAKE_SECONDS
            )

    def blocked(self, sig: str, response: httpx.Response) -> float:
        """Record a 429 and return how long to wait."""
        # Some 429s come from a limiter deep in the game servers and carry no
        # rate-limit headers at all — CCP's docs say so explicitly — hence the
        # fallback group key and default delay.
        group = response.headers.get("x-ratelimit-group") or self._group_of.get(sig) or sig
        self._group_of[sig] = group
        try:
            delay = float(response.headers.get("retry-after", ""))
        except ValueError:
            delay = 60.0
        delay = max(1.0, min(delay, 300.0))
        self._pause_until[group] = max(
            self._pause_until.get(group, 0.0), time.monotonic() + delay
        )
        return delay


_TOKEN_LIMIT = _TokenBucketGovernor()


# --- per-entity 4XX quarantine -----------------------------------------------
# A revoked or de-scoped token answers 401/403 to every authenticated call for
# that character, forever, and each one costs 5 tokens out of the error budget
# that CCP keeps for the whole client. One pilot who removed the app in-game can
# therefore error-limit everybody else's requests — the shared-budget failure the
# error-limit governor above can only react to after the fact.
#
# So: count 4XXs per entity, and once an entity looks broken, stop putting its
# requests on the wire at all. The transport answers them locally with the last
# refusal it actually saw. Nothing is spent, and the rest of the app is
# unaffected.
#
# Keyed on the entity in the URL rather than on the token, because a request
# does not carry an identity the transport can read — the bearer token is opaque
# and /corporations/ calls are made with some member's token.
class _EntityQuarantine:
    #: Consecutive 401/403s before an entity is held back.
    STRIKES = 3
    #: Backoff after each quarantine, in seconds. The last value repeats.
    BACKOFF = (60.0, 300.0, 1800.0, 3600.0)

    def __init__(self) -> None:
        self._strikes: dict[str, int] = {}
        self._held_until: dict[str, float] = {}
        self._rounds: dict[str, int] = {}
        self._last_status: dict[str, int] = {}

    @staticmethod
    def key(url: httpx.URL) -> Optional[str]:
        """`characters/95123456` or `corporations/98000001`, else None.

        Only the authenticated per-entity families are tracked. A 403 on a
        public route is about the route, not about anybody's token.
        """
        parts = [p for p in url.path.split("/") if p]
        for i, seg in enumerate(parts[:-1]):
            if seg in ("characters", "corporations") and parts[i + 1].isdigit():
                return f"{seg}/{parts[i + 1]}"
        return None

    def held(self, key: Optional[str]) -> bool:
        if key is None:
            return False
        if time.monotonic() < self._held_until.get(key, 0.0):
            return True
        # Window passed: let exactly one request through to see if it is fixed.
        if key in self._held_until:
            del self._held_until[key]
            self._strikes[key] = self.STRIKES - 1
        return False

    def last_status(self, key: str) -> int:
        return self._last_status.get(key, 403)

    def refused(self, key: Optional[str], status: int) -> None:
        if key is None:
            return
        self._last_status[key] = status
        self._strikes[key] = self._strikes.get(key, 0) + 1
        if self._strikes[key] < self.STRIKES:
            return
        round_ = self._rounds.get(key, 0)
        delay = self.BACKOFF[min(round_, len(self.BACKOFF) - 1)]
        self._rounds[key] = round_ + 1
        self._held_until[key] = time.monotonic() + delay
        print(f"[esi] {key} answered {status} {self._strikes[key]}x — holding its "
              f"requests for {delay:.0f}s so they stop spending the shared error budget",
              flush=True)

    def succeeded(self, key: Optional[str]) -> None:
        """Any 2xx clears the entity: the token was replaced, or the scope came back."""
        if key is None:
            return
        self._strikes.pop(key, None)
        self._held_until.pop(key, None)
        self._rounds.pop(key, None)
        self._last_status.pop(key, None)

    def reset(self) -> None:
        self._strikes.clear()
        self._held_until.clear()
        self._rounds.clear()
        self._last_status.clear()


_QUARANTINE = _EntityQuarantine()


# --- conditional requests -----------------------------------------------------
# ESI answers every GET with an ETag and honours If-None-Match. A 304 costs one
# token of the error budget instead of two, carries no body, and returns in a
# fraction of the time — and most of what this app fetches (assets, blueprints,
# skills, contracts) changes far less often than it is asked for.
#
# Doing it here rather than at each call site means every fetch gets it, and no
# caller has to learn about 304: a hit is replayed as the 200 it was, with the
# headers that mattered — X-Pages above all, since the paginated fetchers read
# it to decide how many more requests to make.
#
# `app/market/prices.py` keeps its own ETag store, persisted, because market
# history needs the *previous body* recomputed against a moving 7-day window
# rather than replayed. It sets If-None-Match itself, and a request that already
# carries one is left alone.
#: Headers that describe the *encoding of the bytes we were given*, and which
#: therefore stop being true the moment we hand on a decoded body.
_ENCODING_HEADERS = ("content-encoding", "content-length")


def _decoded_headers(response: httpx.Response) -> list[tuple[str, str]]:
    """The response's headers, minus the ones that describe its wire encoding.

    `Response.aread()` decodes: it iterates `aiter_bytes()`, which runs the
    content decoder, so at transport level it returns *decompressed* bytes even
    though the transport is nominally the raw layer. Handing those bytes back
    under the original `Content-Encoding: gzip` makes the client layer try to
    gunzip plain JSON, and httpx raises

        DecodingError: Error -3 while decompressing data: incorrect header check

    which is what the sync worker reported for a real character. No test caught
    it because no stub had ever set `Content-Encoding` — ESI does, on anything
    big enough to be worth compressing, which is exactly the assets and
    blueprints calls the worker makes. `Content-Length` goes too: it counted the
    compressed bytes, and httpx recomputes it from the content it is given.
    """
    return [(name, value) for name, value in response.headers.multi_items()
            if name.lower() not in _ENCODING_HEADERS]


class _ETagCache:
    #: Total body bytes held. Assets for a large character run to a few MB.
    MAX_BYTES = 32 * 1024 * 1024
    #: Bodies larger than this are not worth holding; they are also the rarest.
    MAX_ENTRY = 4 * 1024 * 1024
    #: Replayed on a hit. Everything else is regenerated or irrelevant.
    KEEP_HEADERS = ("content-type", "x-pages", "expires", "last-modified")

    def __init__(self) -> None:
        self._entries: "OrderedDict[str, tuple[str, bytes, list[tuple[str, str]]]]" = (
            OrderedDict()
        )
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(request: httpx.Request) -> str:
        """URL plus a fingerprint of the credential.

        Two characters asking for /characters/{id}/assets/ use different URLs,
        but corp endpoints do not — the same /corporations/{id}/assets/ answers
        differently depending on whose token asked, and roles differ per member.
        The token itself is never stored.
        """
        auth = request.headers.get("authorization", "")
        who = hashlib.sha256(auth.encode()).hexdigest()[:16] if auth else "-"
        return f"{request.method} {request.url} {who}"

    def etag_for(self, key: str) -> Optional[str]:
        entry = self._entries.get(key)
        return entry[0] if entry else None

    def replay(self, key: str, request: httpx.Request) -> Optional[httpx.Response]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        _etag, body, headers = entry
        self.hits += 1
        return httpx.Response(
            200, request=request, content=body,
            headers=headers + [("x-eve-retroindustry-etag", "hit")],
        )

    def store(self, key: str, response: httpx.Response, body: bytes) -> None:
        etag = response.headers.get("etag")
        if not etag or len(body) > self.MAX_ENTRY:
            return
        headers = [(name, response.headers[name])
                   for name in self.KEEP_HEADERS if name in response.headers]
        if key in self._entries:
            self._bytes -= len(self._entries[key][1])
        self._entries[key] = (etag, body, headers)
        self._entries.move_to_end(key)
        self._bytes += len(body)
        self.misses += 1
        while self._bytes > self.MAX_BYTES and len(self._entries) > 1:
            _dropped, (_e, dropped_body, _h) = self._entries.popitem(last=False)
            self._bytes -= len(dropped_body)

    def drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= len(entry[1])

    def reset(self) -> None:
        self._entries.clear()
        self._bytes = 0
        self.hits = self.misses = 0

    def stats(self) -> dict:
        return {"entries": len(self._entries), "bytes": self._bytes,
                "hits": self.hits, "misses": self.misses}


_ETAGS = _ETagCache()


def etag_stats() -> dict:
    """How much the conditional-request cache is saving. For tests and health views."""
    return _ETAGS.stats()


def quarantine_state() -> dict[str, float]:
    """Entities currently held back, and for how many more seconds. For tests
    and for whatever ends up reporting sync health."""
    now = time.monotonic()
    return {k: round(v - now, 1) for k, v in _QUARANTINE._held_until.items()
            if v > now}


def _limit_header_total(value: Optional[str]) -> Optional[int]:
    """X-Ratelimit-Limit is "<tokens>/<window>", e.g. "12000/15m" — take the tokens."""
    if not value:
        return None
    try:
        return int(value.split("/", 1)[0])
    except (ValueError, AttributeError):
        return None


def _int_header(response: httpx.Response, name: str) -> Optional[int]:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError, TypeError):
        return None


# --- market bucket isolation --------------------------------------------------
# Public routes are bucketed by <sourceIP>, which a desktop user shares with every
# other EVE tool on the machine (and, behind CGNAT, with strangers). Supplying an
# access token moves us to <sourceIP>:<applicationID> — the same size bucket, but
# ours alone. It does NOT multiply the budget: per CCP's docs only *authenticated
# routes* key on characterID, so rotating characters buys nothing here.
_market_token_provider: Optional[object] = None
_market_token_disabled_until = 0.0


def set_market_token_provider(fn) -> None:
    """Register a callable returning a valid access token, or None.

    Optional: without it market calls stay unauthenticated and simply share the
    per-IP bucket, exactly as before.
    """
    global _market_token_provider
    _market_token_provider = fn


def _market_auth_header(request: httpx.Request) -> Optional[str]:
    """Token to isolate the bucket for a public market call, or None."""
    if _market_token_provider is None or time.monotonic() < _market_token_disabled_until:
        return None
    if "/markets/" not in request.url.path or "authorization" in request.headers:
        return None
    try:
        token = _market_token_provider()
    except Exception:
        return None
    return f"Bearer {token}" if token else None


class _GovernedTransport(httpx.AsyncHTTPTransport):
    """Wraps the default async transport with both ESI rate-limit governors.

    Only ESI hosts are governed; GitHub/image/Fuzzwork traffic passes straight
    through. A 420 (old error limit, client-wide) pauses all ESI traffic; a 429
    (token bucket, per group) pauses only that group. Both are retried a few
    times before the status is finally handed back so raise_for_status fires.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        global _market_token_disabled_until
        if request.url.host != ESI_HOST:
            return await super().handle_async_request(request)

        sig = _TokenBucketGovernor.signature(request.url)

        # Answer for a held-back entity without touching the network. Its last
        # real refusal is replayed, so callers see the same status they would
        # have got — they already handle it — but it costs nothing.
        entity = _EntityQuarantine.key(request.url)
        if _QUARANTINE.held(entity):
            return httpx.Response(
                _QUARANTINE.last_status(entity),
                request=request,
                headers={"X-Eve-Retroindustry-Quarantined": entity or ""},
                json={"error": "token rejected repeatedly; requests for this "
                               "entity are paused"},
            )

        added_auth = False
        auth = _market_auth_header(request)
        if auth:
            request.headers["Authorization"] = auth
            added_auth = True

        # Ask conditionally when we have seen this exact request before. Set
        # after the market token, because that token is part of the cache key.
        etag_key = None
        if request.method == "GET" and "if-none-match" not in request.headers:
            etag_key = _ETagCache.key(request)
            known = _ETAGS.etag_for(etag_key)
            if known:
                request.headers["If-None-Match"] = known

        last: Optional[httpx.Response] = None
        for attempt in range(4):
            await _ERROR_LIMIT.wait()
            await _TOKEN_LIMIT.wait(sig)
            response = await super().handle_async_request(request)

            # A stale or wrong token must never break a call that would have
            # worked unauthenticated — drop the header, stop using it for a
            # while, and retry as an anonymous request.
            if added_auth and response.status_code in (401, 403):
                await response.aread()
                await response.aclose()
                del request.headers["Authorization"]
                added_auth = False
                _market_token_disabled_until = time.monotonic() + 600
                print("[esi] market token rejected — falling back to unauthenticated "
                      "market calls for 10 minutes", flush=True)
                continue

            reset = _int_header(response, "x-esi-error-limit-reset")
            if response.status_code == 420:
                _ERROR_LIMIT.blocked(reset)
                # Drain + close so the connection can be reused on retry.
                await response.aread()
                await response.aclose()
                last = response
                continue
            if response.status_code == 429:
                delay = _TOKEN_LIMIT.blocked(sig, response)
                await response.aread()
                await response.aclose()
                last = response
                print(f"[esi] 429 on {sig} — waiting {delay:.0f}s", flush=True)
                continue

            _ERROR_LIMIT.observe(_int_header(response, "x-esi-error-limit-remain"), reset)
            _TOKEN_LIMIT.observe(sig, response)

            # 401/403 on a per-entity route means that entity's token, not a
            # transient failure — counted so a permanently broken character
            # stops spending the budget everyone shares. A 404 is not counted:
            # "this character has no jobs" is an ordinary answer.
            if response.status_code in (401, 403):
                _QUARANTINE.refused(entity, response.status_code)
            elif response.is_success:
                _QUARANTINE.succeeded(entity)

            if etag_key is not None:
                if response.status_code == 304:
                    await response.aread()
                    await response.aclose()
                    replayed = _ETAGS.replay(etag_key, request)
                    if replayed is not None:
                        _QUARANTINE.succeeded(entity)   # 304 means the token worked
                        return replayed
                    # The entry went away between asking and answering; without
                    # a body there is nothing to hand back, so ask again
                    # unconditionally rather than returning an empty 304.
                    del request.headers["If-None-Match"]
                    continue
                if response.status_code == 200:
                    body = await response.aread()
                    _ETAGS.store(etag_key, response, body)
                    return httpx.Response(200, request=request, content=body,
                                          headers=_decoded_headers(response))
                _ETAGS.drop(etag_key)
            return response
        return last  # exhausted retries — hand it back so raise_for_status fires


def esi_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient with a preset X-Compatibility-Date header, a
    connection pool sized for our concurrency (see _ESI_LIMITS), and a shared
    ESI error-limit governor (see _ErrorLimitGovernor). For non-ESI hosts
    (GitHub, images) both header and governor are harmless. Per-request headers
    are merged with the client header; the caller can override limits via kwargs."""
    headers = {"X-Compatibility-Date": ESI_COMPAT_DATE, "User-Agent": USER_AGENT}
    headers.update(kwargs.pop("headers", None) or {})
    limits = kwargs.pop("limits", _ESI_LIMITS)
    # A custom transport takes over pool sizing, so feed it the limits. Passing
    # both transport= and limits= to AsyncClient would make httpx ignore limits.
    kwargs.setdefault("transport", _GovernedTransport(limits=limits))
    return httpx.AsyncClient(headers=headers, **kwargs)


def esi_error_message(exc: BaseException) -> Optional[str]:
    """Turn a raw httpx error into a short, user-facing ESI message, or None if
    it isn't a recognizable HTTP error. Used to replace httpx's default
    'Client error 420 ... developer.mozilla.org' text in the UI."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (420, 429):
            return ("EVE's ESI API is rate-limiting this app right now "
                    "(too many requests in a short window). Wait a minute and try again.")
        if code in (502, 503, 504):
            return "EVE's ESI API is temporarily unavailable. Try again in a moment."
        if code == 403:
            return "ESI denied access (token expired or missing scope). Re-add the character."
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
        return "Couldn't reach EVE's ESI API (network/timeout). Check your connection and retry."
    return None

# Rate limiting: ESI allows ~150 req/s, Fuzzwork is slower
ESI_SEMAPHORE = asyncio.Semaphore(20)
FUZZ_SEMAPHORE = asyncio.Semaphore(5)


async def fetch_type_info(client: httpx.AsyncClient, type_id: int) -> Optional[dict]:
    """Fetches the type's name and category from ESI."""
    async with ESI_SEMAPHORE:
        r = await client.get(
            f"{ESI_BASE}/universe/types/{type_id}/",
            params={"datasource": "tranquility", "language": "en"},
            timeout=10,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def fetch_blueprint_data(client: httpx.AsyncClient, type_id: int) -> Optional[dict]:
    """
    Fetches blueprint data from the Fuzzwork API.
    type_id is the *product* ID (not the blueprint's).
    Returns manufacturing/reaction activities with a list of materials.
    """
    async with FUZZ_SEMAPHORE:
        r = await client.get(
            f"{FUZZWORK_BASE}/blueprint/",
            params={"typeID": type_id, "format": "json"},
            timeout=15,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        # Fuzzwork returns a dict where the key is the blueprint_type_id
        return data if data else None


async def search_type_by_name(client: httpx.AsyncClient, name: str) -> list[int]:
    """Converts a name to a type_id via ESI /universe/ids/ (POST)."""
    async with ESI_SEMAPHORE:
        r = await client.post(
            f"{ESI_BASE}/universe/ids/",
            params={"datasource": "tranquility", "language": "en"},
            json=[name],
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        types = data.get("inventory_types", [])
        return [t["id"] for t in types]


async def fetch_types_bulk(client: httpx.AsyncClient, type_ids: list[int]) -> dict[int, dict]:
    """Fetches information about multiple types at once."""
    tasks = [fetch_type_info(client, tid) for tid in type_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        tid: res
        for tid, res in zip(type_ids, results)
        if isinstance(res, dict)
    }
