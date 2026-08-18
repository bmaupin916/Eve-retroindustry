"""EVE SSO endpoint discovery.

CCP publishes an OAuth 2.0 Authorization Server Metadata document (RFC 8414) and
says plainly that the endpoint URLs "may change in the future… it is recommended
to always fetch them from the endpoint", cached for a reasonable period. We used
to pin `AUTH_URL` and `TOKEN_URL` as literals; this module replaces that with a
cached fetch. Baseline finding 9.

It is also the prerequisite for verifying access-token signatures, because the
`jwks_uri` comes from the same document — see `app/auth/jwt_verify.py`.

Design notes:

* **Failure is non-fatal.** If the document cannot be fetched we fall back to the
  values that were hardcoded before. Discovery is a correctness and
  future-proofing measure, not an availability dependency — a CCP hiccup or a
  captive portal must not make login impossible when the well-known URLs still
  work.
* **The cache is process-local and time-bounded.** No disk cache: the document is
  tiny, the fetch happens at most once per TTL, and a stale file surviving a
  restart is the exact failure mode discovery exists to avoid.
* **Synchronous on purpose.** Both callers (the CLI flow and the callback thread)
  are synchronous, and this runs off the event loop.
"""
from __future__ import annotations

import threading
import time

import httpx

from app.version import USER_AGENT

DISCOVERY_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"

# "Cached for a reasonable period" — CCP does not name a number. An hour is long
# enough that a login storm costs one fetch, short enough that a real endpoint
# migration is picked up without a restart.
_TTL_SECONDS = 3600

# The values pinned in esi_oauth.py before discovery existed. Kept as the offline
# fallback, and as the answer to "what did this used to do".
_FALLBACK = {
    "issuer": "https://login.eveonline.com",
    "authorization_endpoint": "https://login.eveonline.com/v2/oauth/authorize",
    "token_endpoint": "https://login.eveonline.com/v2/oauth/token",
    "jwks_uri": "https://login.eveonline.com/oauth/jwks",
    "revocation_endpoint": "https://login.eveonline.com/v2/oauth/revoke",
}

_lock = threading.Lock()
_cache: dict | None = None
_cache_at: float = 0.0
_warned = False


def _fetch() -> dict:
    r = httpx.get(
        DISCOVERY_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=10,
        follow_redirects=True,
    )
    r.raise_for_status()
    doc = r.json()
    if not isinstance(doc, dict):
        raise ValueError(f"discovery document is {type(doc).__name__}, not an object")
    # A document missing the fields we actually use is worse than no document —
    # it would silently hand `None` to the token exchange. Treat it as a failure
    # and fall back rather than merging a half-answer over known-good values.
    required = ("authorization_endpoint", "token_endpoint", "jwks_uri")
    missing = [k for k in required if not doc.get(k)]
    if missing:
        raise ValueError(f"discovery document missing {', '.join(missing)}")
    return doc


def get_metadata(force: bool = False) -> dict:
    """Return the SSO metadata document, fetching at most once per TTL.

    Never raises: on any failure the fallback constants are returned, and the
    reason is logged once rather than on every login attempt.
    """
    global _cache, _cache_at, _warned

    with _lock:
        fresh = _cache is not None and (time.time() - _cache_at) < _TTL_SECONDS
        if fresh and not force:
            return _cache

        try:
            doc = _fetch()
        except Exception as exc:
            if not _warned:
                print(f"[sso] discovery fetch failed ({exc!r}); using pinned endpoints",
                      flush=True)
                _warned = True
            # Keep serving a previously-good document if we have one; only drop
            # to the hardcoded fallback when we have never succeeded.
            return _cache if _cache is not None else dict(_FALLBACK)

        _cache, _cache_at, _warned = doc, time.time(), False
        return doc


def _endpoint(key: str) -> str:
    return get_metadata().get(key) or _FALLBACK[key]


def authorization_endpoint() -> str:
    return _endpoint("authorization_endpoint")


def token_endpoint() -> str:
    return _endpoint("token_endpoint")


def jwks_uri() -> str:
    return _endpoint("jwks_uri")


def issuer() -> str:
    return _endpoint("issuer")


def reset_cache() -> None:
    """Drop the cached document. For tests, and for a manual re-read."""
    global _cache, _cache_at, _warned
    with _lock:
        _cache, _cache_at, _warned = None, 0.0, False
