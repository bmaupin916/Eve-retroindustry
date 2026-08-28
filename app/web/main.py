"""FastAPI web application for EVE Retroindustry."""
from __future__ import annotations

from app.version import APP_VERSION  # single source of truth (app/version.py)
from app.web import security

import asyncio
import os
import json
import time as _time

from app.esi.client import (
    esi_client,
    set_market_token_provider as _esi_set_market_token_provider,
)
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

# `list_characters`, `ACTIVE_COOKIE` and `get_conn` below are imported and not used
# *here*. They are deliberate re-exports: the test suite reaches them as
# `app_module.<name>`, so removing them breaks tests rather than tidying
# anything. Everything else in this block was dead weight from the W6 split —
# 113 unused imports, removed in v0.9.76 — and pyflakes should report exactly
# these three and nothing more.
from app.auth.token_store import list_characters, is_refresh_invalid
from app.web.prices_helper import (
    get_prices_for_ids,
    get_cached_prices_for_ids,
    get_price_cache_stats,
)
from app.web.location_resolver import resolve_station_names_bulk
from app.web.industry_helper import populate_rig_bonuses
from app.character.skills import fetch_skill_queue, fetch_location, fetch_ship
from app.db import conn as db_conn
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from app.db.conn import NO_SUCH_TABLE, connect as _connect
from app.db.migrate import upgrade_to_head
from app.db.schema import create_sde_schema
from app.sync import worker as sync_worker

from app.web.deps import (
    all_characters,
    any_character,
    character_row,
    ACTIVE_COOKIE,
    STATIC_DIR,
    _SDE_READY,
    _container_display_name,
    _deny,
    _ensure_groups_populated,
    _isk0,
    _load_assets_from_cache,
    _tr,
    _valid_token_async,
    get_active_character_id,
    get_conn,
)


app = FastAPI(title="EVE Retroindustry")

# Vendored front-end assets (Bootstrap CSS/JS + icons) served locally —
# no CDN dependency (important for Android WebView + offline desktop).
if STATIC_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Set EVE_DEBUG_ERRORS=1 to put the traceback back in the HTTP response. Off by
# default: hosted, the response body goes to whoever made the request, and these
# tracebacks carry absolute filesystem paths, SQL and local variable values. The
# server log keeps the full detail either way, which is where it was actually
# useful for debugging console=False desktop bundles. (Baseline finding 6.)
_DEBUG_ERRORS = bool(os.environ.get("EVE_DEBUG_ERRORS"))


@app.exception_handler(Exception)
async def _log_unhandled(request: Request, exc: Exception):
    """Log every uncaught exception with its traceback; return an opaque 500."""
    import traceback
    tb = traceback.format_exc()
    print(f"[error] {request.method} {request.url.path} -> {type(exc).__name__}: {exc}\n{tb}",
          flush=True)
    if _DEBUG_ERRORS:
        return PlainTextResponse(f"Internal Server Error\n\n{type(exc).__name__}: {exc}\n\n{tb}",
                                 status_code=500)
    return PlainTextResponse("Internal Server Error", status_code=500)


@app.middleware("http")
async def _setup_gate(request: Request, call_next):
    """Redirect every request to /setup until SDE data is available.

    Public auth paths are exempt, or a fresh install deadlocks: /setup needs a
    session, and getting a session needs /auth/login, which this would bounce
    back to /setup.
    """
    path = request.url.path
    if not _SDE_READY[0] and not path.startswith("/setup")             and not security.is_public_path(path):
        return RedirectResponse("/setup")
    return await call_next(request)


# Registered after _setup_gate, so it runs *before* it: Starlette wraps each new
# middleware around the previous one. Host and identity are checked before the
# app decides whether it has data to show.
@app.middleware("http")
async def _security_gate(request: Request, call_next):
    """Host validation, session authentication and CSRF, in that order.

    Baseline findings 1 and 2. The three belong together because each depends on
    the one before: a Host check makes the origin trustworthy, a session makes
    the caller known, and the CSRF token is bound to that session.
    """
    # 1. Host. Rejecting an unexpected Host is what actually stops DNS
    #    rebinding — the browser will happily send our own cookies to a name
    #    the attacker controls that resolves to loopback, and only the Host
    #    header distinguishes that from a real visit.
    if not security.host_is_allowed(request.headers.get("host")):
        return PlainTextResponse("Bad Host header", status_code=400)

    path = request.url.path
    if security.is_public_path(path):
        return await call_next(request)

    # `security` is on the portable query layer, so this is an engine connection
    # rather than the raw handle. It runs on **every** non-public request, which
    # is why it is a short `with` and not something held across `call_next`.
    with _connect() as _sc:
        session = security.load_session(
            _sc, request.cookies.get(security.SESSION_COOKIE))

    if session is None:
        return _deny(request, 401, "Not authenticated")

    # 2. CSRF on anything that changes state. The token lives in the session and
    #    arrives either as a header (the fetch wrapper in base.html) or as a form
    #    field (the server-rendered forms). A session cookie alone is not enough,
    #    which is the entire point.
    if request.method in security._UNSAFE_METHODS:
        form_token = None
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            # body() before form(): reading the body here would otherwise leave
            # the route with an exhausted stream and every form POST failing 422.
            # Starlette's BaseHTTPMiddleware replays a cached `_body` downstream,
            # but only when body() was the thing that consumed it — form() alone
            # marks the stream consumed without caching anything to replay.
            await request.body()
            form = await request.form()
            form_token = form.get(security.CSRF_FIELD)
        if not security.csrf_ok(session["csrf_token"],
                                request.headers.get(security.CSRF_HEADER),
                                form_token):
            return _deny(request, 403, "CSRF token missing or invalid")

    request.state.session = session
    return await call_next(request)


@app.on_event("startup")
async def _startup_populate_groups():
    """Check SDE readiness, refresh from bundled DB if outdated, then
    load group names and rig bonuses."""
    # Deploying is `git pull` and a restart; the schema catches itself up.
    try:
        revision = upgrade_to_head()
        print(f"[db] schema at revision {revision}", flush=True)
    except Exception as exc:
        # Not fatal on its own: `ensure_schema()` still creates anything
        # missing, so the app comes up. But it means the deployment is no
        # longer tracking revisions, which is worth shouting about.
        print(f"[db] MIGRATION FAILED — schema may be behind: {exc}", flush=True)

    # The SDE is deliberately outside the migration history — it is replaced
    # wholesale on every build and carries no user data — so `upgrade_to_head`
    # above does not create it and something here has to. On SQLite
    # `ensure_schema()` has always done it lazily through `get_conn()`; that
    # path is `sqlite3`-only, so on any other backend these tables did not
    # exist at all and the row count three lines below raised `UndefinedTable`.
    #
    # Empty is the right state to leave them in. `import_sde.py` is what fills
    # them, and a query against an empty table returns no rows, whereas a query
    # against a missing one takes the page down.
    try:
        create_sde_schema(db_conn.engine())
    except Exception as exc:
        print(f"[sde] could not create the static-data tables: {exc}", flush=True)

    try:
        with _connect() as conn:
            try:
                count = conn.execute(
                    text("SELECT COUNT(*) FROM sde_types")).fetchone()[0]
            except NO_SUCH_TABLE:
                # Portable pair, not `sqlite3.OperationalError`: this runs on a
                # database that may have no SDE tables yet, and Postgres raises
                # ProgrammingError for the same condition.
                conn.rollback()
                count = 0

        _SDE_READY[0] = count > 0
        if _SDE_READY[0]:
            # industry_helper is on the portable query layer now, so it gets an
            # engine connection rather than this startup's raw handle.
            with _connect() as _ic:
                populate_rig_bonuses(_ic)
            with _connect() as _gc:
                await _ensure_groups_populated(_gc)
        else:
            # Nothing here copies a database into place any more. Static data
            # arrives by running the importer against CCP's build-pinned feed,
            # which is the difference between "as current as our last release"
            # and "current".
            print("[sde] no static data in this database. Run:  python import_sde.py",
                  flush=True)
        conn.close()
    except Exception:
        _SDE_READY[0] = False

    # Last, and outside the try above: a failure to read the SDE must not stop
    # the caches being kept warm, and a failure to start the worker must not
    # take the app down with it.
    try:
        worker = sync_worker.start()
        if worker is not None:
            print(f"[sync] background worker started, every "
                  f"{worker.interval / 60:.0f} min per character", flush=True)
        else:
            print("[sync] background worker disabled (EVE_SYNC_WORKER)", flush=True)
    except Exception as exc:
        print(f"[sync] could not start the background worker: {exc}", flush=True)


@app.on_event("shutdown")
async def _shutdown_sync_worker():
    """Stop the loop so a reload does not leave a second one running."""
    try:
        await sync_worker.stop()
    except Exception:
        pass


_WALLET_CACHE_TTL = 300.0  # 5 minutes


async def _fetch_wallet_balance(
    conn: Connection, char_id: int, token: str | None
) -> float | None:
    """Returns ISK wallet balance, using a 5-min SQLite cache."""
    now = _time.time()
    row = conn.execute(
        text("SELECT balance, cached_at FROM char_wallet_cache"
             " WHERE character_id=:cid"), {"cid": char_id},
    ).fetchone()
    if row and (now - row[1]) < _WALLET_CACHE_TTL:
        return row[0]
    if not token:
        return row[0] if row else None
    try:
        async with esi_client(timeout=8) as client:
            r = await client.get(
                f"https://esi.evetech.net/latest/characters/{char_id}/wallet/",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                balance = float(r.json())
                conn.execute(
                    text("INSERT INTO char_wallet_cache"
                         " (character_id, balance, cached_at)"
                         " VALUES (:cid, :balance, :cached_at)"
                         " ON CONFLICT (character_id) DO UPDATE SET"
                         " balance=excluded.balance,"
                         " cached_at=excluded.cached_at"),
                    {"cid": char_id, "balance": balance, "cached_at": now},
                )
                conn.commit()
                return balance
    except Exception:
        pass
    return row[0] if row else None


_market_token_cache: dict = {"token": None, "until": 0.0}


def _market_bucket_token() -> str | None:
    """An already-valid access token, used only to isolate our market bucket.

    Public market routes are bucketed by <sourceIP>, shared with every other EVE
    tool on the machine; sending a token makes it <sourceIP>:<applicationID>, so
    the budget is ours alone. It grants no extra budget and no extra access, so
    which character it belongs to is irrelevant.

    This runs INSIDE the async transport, on the event loop, so it must never
    block. It therefore reads a stored token straight from the table and never
    calls get_valid_token(), which performs a synchronous OAuth refresh when the
    token has expired — doing that here stalled the whole server, dashboard
    included, for the length of a round trip to the SSO endpoint. (The codebase
    already had _valid_token_async for exactly this reason.) The result is cached
    briefly so a burst of market pages costs one query, not one per request.
    """
    now = _time.time()
    if now < _market_token_cache["until"]:
        return _market_token_cache["token"]
    token: str | None = None
    try:
        with _connect() as conn:
            row = conn.execute(
                text("SELECT access_token FROM characters"
                     " WHERE access_token IS NOT NULL"
                     "   AND token_expires_at > :cutoff"
                     " ORDER BY token_expires_at DESC LIMIT 1"),
                # margin, so it cannot expire mid-flight
                {"cutoff": now + 30},
            ).fetchone()
            token = row[0] if row and row[0] else None
    except Exception:
        token = None
    # Cached even when None: with nobody logged in this would otherwise hit the
    # DB on every single market request.
    _market_token_cache.update({"token": token, "until": now + 60})
    return token


_esi_set_market_token_provider(_market_bucket_token)


# Dashboard
# ---------------------------------------------------------------------------

def _roman(n: int) -> str:
    return {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}.get(n, str(n))


def _fmt_remaining(finish_iso: str, now) -> str:
    """'2d 3h 15m' until the end; '' on error."""
    import datetime as _dt
    try:
        end = _dt.datetime.fromisoformat(finish_iso.replace("Z", "+00:00"))
        secs = int((end - now).total_seconds())
        if secs <= 0:
            return "done"
        d, r = divmod(secs, 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        return (f"{d}d " if d else "") + (f"{h}h " if (d or h) else "") + f"{m}m"
    except Exception:
        return ""


async def _compute_dashboard(request: Request, conn, *, live: bool) -> dict:
    """Build the dashboard context.

    live=False → everything from the local cache/DB, NO ESI call, renders
    instantly. live=True → also fetch the ESI-backed fields (corp/system names,
    wallet, location, skill queue, adjusted prices); used by /api/dashboard/live.
    Splitting it this way means a slow or rate-limited ESI can never make the
    dashboard (or the whole app) appear frozen.
    """
    logged_in = any_character()
    price_stats: dict = {}
    char_cards: list[dict] = []
    corp_names: dict[int, str] = {}
    agg_bps = agg_assets = agg_locations = 0
    agg_value: float | None = None
    agg_wallet: float | None = None

    if not logged_in:
        return {
            "logged_in": False, "char_cards": [], "agg_bps": 0, "agg_assets": 0,
            "agg_locations": 0, "agg_value": None, "agg_wallet": None,
            "price_stats": price_stats, "live_pending": False,
        }

    chars = all_characters()
    active_char_id = get_active_character_id(request)
    char_rows: dict[int, dict] = {cid: (character_row(cid) or {}) for cid, _ in chars}

    # Access tokens — fetched once per char, OFF the event loop (live only).
    # return_exceptions so one char's refresh error can't blow up the endpoint.
    tokens: dict[int, str | None] = {}
    if live:
        _cids = [cid for cid, _ in chars]
        _tok_res = await asyncio.gather(*[_valid_token_async(c) for c in _cids], return_exceptions=True)
        tokens = {c: (t if isinstance(t, str) else None) for c, t in zip(_cids, _tok_res)}

    # Corporation names via ESI bulk (live only).
    if live:
        corp_ids = list({row.get("corporation_id") for row in char_rows.values() if row.get("corporation_id")})
        if corp_ids:
            try:
                async with esi_client(timeout=5) as client:
                    r = await client.post(
                        "https://esi.evetech.net/latest/universe/names/",
                        json=corp_ids,
                        headers={"Accept": "application/json"},
                    )
                    if r.status_code == 200:
                        for item in r.json():
                            corp_names[item["id"]] = item["name"]
            except Exception:
                pass

    # Assets from cache (always).
    # all_assets_by_char: everything incl. singletons — for value calculation
    # assets_by_char: non-singletons only — for location/count display stats
    all_type_ids_set: set[int] = set()
    assets_by_char: dict[int, list[dict]] = {}
    all_assets_by_char: dict[int, list[dict]] = {}
    for cid, _ in chars:
        with _connect() as _ac:
            raw = _load_assets_from_cache(_ac, cid)
        all_assets_by_char[cid] = raw
        assets_by_char[cid] = [a for a in raw if not a.get("is_singleton", False)]
        all_type_ids_set.update(a["type_id"] for a in raw)

    # Prices: full (ESI adjusted for missing) when live, cache-only otherwise.
    prices: dict[int, tuple] = {}
    if all_type_ids_set:
        # Both branches call into `prices_helper`, which is on the portable
        # query layer now. Rewiring only the `live` one left the cache-only path
        # — the one the dashboard actually takes on a normal load — handing a
        # DBAPI connection to a converted function.
        with _connect() as _pc:
            if live:
                prices = await get_prices_for_ids(_pc, list(all_type_ids_set))
            else:
                prices = get_cached_prices_for_ids(_pc, list(all_type_ids_set))

    # Blueprint group_ids — exclude from net worth (matches in-game behavior)
    bp_group_ids: set[int] = {
        r[0] for r in conn.execute(text(
            "SELECT group_id FROM sde_groups WHERE name LIKE '%Blueprint%'"
        )).fetchall()
    }
    type_group: dict[int, int] = {
        r[0]: r[1] for r in conn.execute(
            text("SELECT type_id, group_id FROM sde_types"
                 " WHERE type_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": list(all_type_ids_set)},
        ).fetchall()
    } if all_type_ids_set else {}

    # Wallet balances (live only, 5-min cache).
    wallet_balances: dict[int, float | None] = {cid: None for cid, _ in chars}
    if live:
        _cids = [cid for cid, _ in chars]
        wallet_balances = dict(zip(_cids, await asyncio.gather(*[
            _fetch_wallet_balance(conn, c, tokens.get(c)) for c in _cids
        ])))
        _wallets = [w for w in wallet_balances.values() if w is not None]
        if _wallets:
            agg_wallet = sum(_wallets)

    # Current location + skill training (live only, concurrently for all chars).
    import datetime as _dt
    _now_utc = _dt.datetime.now(_dt.timezone.utc)
    loc_sq: dict[int, tuple] = {cid: ({}, [], {}) for cid, _ in chars}
    dock_names: dict[int, str] = {}
    sys_names: dict[int, str] = {}
    skill_names: dict[int, str] = {}
    if live:
        _char_ids = [cid for cid, _ in chars]

        async def _fetch_loc_sq(cid: int):
            tok = tokens.get(cid)
            if not tok:
                return {}, [], {}
            async with esi_client() as client:
                # The ship is fetched for everyone rather than only after seeing
                # "undocked", which would need a second round trip. It is one
                # extra call per character into the char-location group (600
                # requests per 15 min, per character), so the cost is noise.
                return await asyncio.gather(
                    fetch_location(client, cid, tok),
                    fetch_skill_queue(client, cid, tok),
                    fetch_ship(client, cid, tok),
                )

        _loc_res = await asyncio.gather(*[_fetch_loc_sq(c) for c in _char_ids], return_exceptions=True)
        loc_sq = {
            c: (r if isinstance(r, (list, tuple)) and len(r) == 3 else ({}, [], {}))
            for c, r in zip(_char_ids, _loc_res)
        }

        try:
            _tok_ok = sum(1 for t in tokens.values() if t)
            _loc_ok = sum(1 for v in loc_sq.values() if v[0])
            _trn_ok = sum(1 for v in loc_sq.values() if v[1])
            print(f"[dash-live] chars={len(chars)} tokens_ok={_tok_ok}/{len(chars)} "
                  f"located={_loc_ok} training={_trn_ok}", flush=True)
        except Exception:
            pass

        _dock_ids: set[int] = set()
        _sys_ids: set[int] = set()
        _skill_ids: set[int] = set()
        for _cid in _char_ids:
            _loc, _sq, _ship = loc_sq.get(_cid, ({}, [], {}))
            if _loc.get("station_id"):
                _dock_ids.add(_loc["station_id"])
            if _loc.get("structure_id"):
                _dock_ids.add(_loc["structure_id"])
            if _loc.get("solar_system_id"):
                _sys_ids.add(_loc["solar_system_id"])
            # Every skill in the queue, not just entry 0. ESI parks an already
            # finished skill at position 0 until the pilot next logs in, so the
            # entry actually displayed is the first one finishing in the FUTURE
            # (v0.8.114). Collecting only [0] resolved the name of a skill that is
            # not shown and left the displayed one as a bare "#3304". Taking the
            # whole queue cannot drift from the display rule again, and it is one
            # local SQL IN over a handful of ids.
            for _e in _sq or ():
                if _e.get("skill_id"):
                    _skill_ids.add(_e["skill_id"])

        if _dock_ids:
            _any_tok = next((t for t in tokens.values() if t), None)
            try:
                with _connect() as _lc:
                    dock_names = await resolve_station_names_bulk(
                        list(_dock_ids), token=_any_tok, conn=_lc)
            except Exception:
                dock_names = {}
        if _sys_ids:
            try:
                async with esi_client(timeout=5) as client:
                    rr = await client.post(
                        "https://esi.evetech.net/latest/universe/names/",
                        json=list(_sys_ids), headers={"Accept": "application/json"},
                    )
                    if rr.status_code == 200:
                        for it in rr.json():
                            sys_names[it["id"]] = it["name"]
            except Exception:
                pass
        if _skill_ids:
            skill_names = {r[0]: r[1] for r in conn.execute(
                text("SELECT type_id, name FROM sde_types"
                     " WHERE type_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": list(_skill_ids)},
            ).fetchall()}

    # Per-character cards.
    for cid, cname in chars:
        char_row = char_rows[cid]
        # Counted in Python rather than by `json_array_length(data_json)`.
        # SQLite applies that function to a TEXT column happily; Postgres has
        # the function but not for `text` — it wants an explicit `::json` cast,
        # so the SQL would have had to differ per backend. One row per
        # character, so reading the blob costs nothing worth a dialect branch.
        bp_row = conn.execute(
            text("SELECT data_json FROM char_blueprints_cache"
                 " WHERE character_id=:cid"), {"cid": cid},
        ).fetchone()
        try:
            bp_count = len(json.loads(bp_row[0])) if bp_row and bp_row[0] else 0
        except (TypeError, ValueError):
            bp_count = 0

        assets = assets_by_char.get(cid, [])         # non-singleton, for counts
        all_assets = all_assets_by_char.get(cid, [])  # all items, for value

        char_value: float | None = None
        # Exclude blueprints from value (matches in-game "Total Net Worth" behavior)
        priced_assets = [
            (a, prices.get(a["type_id"], (None, None))[0])
            for a in all_assets
            if type_group.get(a["type_id"]) not in bp_group_ids
        ]
        priced_sum = sum(p * a.get("quantity", 1) for a, p in priced_assets if p is not None)
        if any(p is not None for _, p in priced_assets):
            char_value = priced_sum

        wallet = wallet_balances.get(cid)
        net_worth: float | None = None
        if char_value is not None or wallet is not None:
            net_worth = (char_value or 0.0) + (wallet or 0.0)

        last_sync_at = char_row.get("last_sync_at")
        corp_id = char_row.get("corporation_id")

        # Location: docked station/structure, or system + "undocked".
        _loc, _sq, _ship = loc_sq.get(cid, ({}, [], {}))
        location_name = None
        location_state = None
        if _loc.get("station_id"):
            location_name = dock_names.get(_loc["station_id"]) or f"#{_loc['station_id']}"
            location_state = "docked"
        elif _loc.get("structure_id"):
            location_name = dock_names.get(_loc["structure_id"]) or f"#{_loc['structure_id']}"
            location_state = "docked"
        elif _loc.get("solar_system_id"):
            location_name = sys_names.get(_loc["solar_system_id"]) or f"#{_loc['solar_system_id']}"
            location_state = "undocked"

        # Which hull the pilot is flying, labelled exactly like an assembled ship
        # in Assets: "custom name (Type)". ESI's ship_name is whatever they
        # renamed it to, so on its own it can be anything ("Hulk1", "Rorq") and
        # would not say what is actually out there.
        ship_label = None
        _ship_type = _ship.get("ship_type_id")
        if _ship_type:
            _ship_type_name = conn.execute(
                text("SELECT name FROM sde_types WHERE type_id=:tid"),
                {"tid": _ship_type},
            ).fetchone()
            ship_label = _container_display_name(
                _ship.get("ship_name") or "",
                _ship_type_name[0] if _ship_type_name else "",
                _ship.get("ship_item_id") or 0,
            )

        # Active training: the first queue entry still training (finish_date in
        # the FUTURE). ESI keeps already-completed skills in the queue at
        # position 0 (with a past finish_date) until the character next logs in,
        # so taking _sq[0] showed a finished skill as "done" and never surfaced
        # the one actually training now. Skip the finished ones.
        training = None
        _act = None
        for _e in _sq:
            _fd = _e.get("finish_date")
            if not _fd:
                continue
            try:
                if _dt.datetime.fromisoformat(_fd.replace("Z", "+00:00")) > _now_utc:
                    _act = _e
                    break
            except Exception:
                continue
        if _act and _act.get("skill_id") and _act.get("finish_date"):
            # SP/hour for the skill in training. Derived straight from the queue
            # entry — SP gained over its wall-clock span — so it already reflects
            # this character's attributes, attribute implants and any boosters,
            # and it's specific to the current skill's primary/secondary attrs.
            sp_hr = None
            try:
                if _act.get("start_date"):
                    _s = _dt.datetime.fromisoformat(_act["start_date"].replace("Z", "+00:00"))
                    _f = _dt.datetime.fromisoformat(_act["finish_date"].replace("Z", "+00:00"))
                    _hrs = (_f - _s).total_seconds() / 3600.0
                    _sp = (_act.get("level_end_sp") or 0) - (
                        _act.get("training_start_sp") or _act.get("level_start_sp") or 0)
                    if _hrs > 0 and _sp > 0:
                        sp_hr = round(_sp / _hrs)
            except Exception:
                sp_hr = None
            training = {
                "skill":     skill_names.get(_act["skill_id"], f"#{_act['skill_id']}"),
                "level":     _roman(_act.get("finished_level", 0)),
                "remaining": _fmt_remaining(_act["finish_date"], _now_utc),
                "finish_iso": _act["finish_date"],   # live countdown on the client
                "sp_per_hour":     sp_hr,
                "sp_per_hour_str": (f"{sp_hr:,}".replace(",", " ") if sp_hr else None),
            }

        char_cards.append({
            "char_id":     cid,
            "char_name":   cname,
            "corp_id":     corp_id,
            "corp_name":   corp_names.get(corp_id, "") if corp_id else "",
            "asset_value": char_value,
            "wallet":      wallet,
            "net_worth":   net_worth,
            "last_sync_at": last_sync_at,
            "is_active":   cid == active_char_id,
            "location_name":  location_name,
            "location_state": location_state,
            "ship_label":     ship_label,
            "training":       training,
            "needs_relogin":  is_refresh_invalid(cid),
        })

        agg_bps += bp_count
        agg_assets += len(assets)
        agg_locations = len({loc for c in assets_by_char.values() for a in c for loc in [a["location_id"]]})
        if net_worth is not None:
            agg_value = (agg_value or 0.0) + net_worth
        elif char_value is not None:
            agg_value = (agg_value or 0.0) + char_value

    with _connect() as _pc:
        price_stats = get_price_cache_stats(_pc)

    return {
        "logged_in": True,
        "char_cards": char_cards,
        "agg_bps": agg_bps,
        "agg_assets": agg_assets,
        "agg_locations": agg_locations,
        "agg_value": agg_value,
        "agg_wallet": agg_wallet,
        "price_stats": price_stats,
        "live_pending": not live,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Instant render from cache/DB only — no ESI. The ESI-backed fields load
    afterwards via /api/dashboard/live, so a slow or rate-limited ESI can never
    make the dashboard (and the app) look frozen."""
    with _connect() as conn:
        ctx = await _compute_dashboard(request, conn, live=False)
    ctx["login_busy"] = request.query_params.get("login_busy") == "1"
    return _tr("index.html", request, ctx)


@app.get("/api/dashboard/live")
async def api_dashboard_live(request: Request):
    """ESI-backed dashboard data (corp names, wallet, location, skill queue,
    refined prices), fetched by the dashboard right after the instant render."""
    with _connect() as conn:
        ctx = await _compute_dashboard(request, conn, live=True)
    if not ctx["logged_in"]:
        return {"logged_in": False}

    def _s(v):
        # Whole ISK on the dashboard: at billions the cents are pure noise and
        # they push the numbers wide enough to wrap on a character card.
        return _isk0(v) if v is not None else None

    chars_out: dict[str, dict] = {}
    for c in ctx["char_cards"]:
        chars_out[str(c["char_id"])] = {
            "corp_name":       c["corp_name"],
            "wallet_str":      _s(c["wallet"]),
            "asset_value_str": _s(c["asset_value"]),
            "net_worth_str":   _s(c["net_worth"]),
            "has_worth":       c["net_worth"] is not None or c["asset_value"] is not None,
            "location_name":   c["location_name"],
            "location_state":  c["location_state"],
            "ship_label":      c.get("ship_label"),
            "training":        c["training"],
            "needs_relogin":   c["needs_relogin"],
        }
    return {
        "logged_in": True,
        "agg_wallet_str": _s(ctx["agg_wallet"]),
        "agg_value_str": _isk0(ctx["agg_value"]) if ctx["agg_value"] else None,
        "chars": chars_out,
    }


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return _tr("about.html", request, {"version": APP_VERSION})


# ---------------------------------------------------------------------------
# Routers (W6). Included at the end so registration order matches the order the
# routes were declared in before the split — FastAPI matches in registration
# order, and a router included above a path it overlaps would start winning it.
# ---------------------------------------------------------------------------

from app.web.routers import prices as prices_router  # noqa: E402
from app.web.routers import assets as assets_router  # noqa: E402
from app.web.routers import auth as auth_router  # noqa: E402
from app.web.routers import characters as characters_router  # noqa: E402
from app.web.routers import contracts as contracts_router  # noqa: E402
from app.web.routers import industry as industry_router  # noqa: E402
from app.web.routers import locations as locations_router  # noqa: E402
from app.web.routers import media as media_router  # noqa: E402
from app.web.routers import plan as plan_router  # noqa: E402
from app.web.routers import planets as planets_router  # noqa: E402
from app.web.routers import projects as projects_router  # noqa: E402
from app.web.routers import sync_health as sync_health_router  # noqa: E402

app.include_router(assets_router.router)
app.include_router(auth_router.router)
app.include_router(characters_router.router)
app.include_router(contracts_router.router)
app.include_router(industry_router.router)
app.include_router(locations_router.router)
app.include_router(media_router.router)
app.include_router(plan_router.router)
app.include_router(planets_router.router)
app.include_router(prices_router.router)
app.include_router(projects_router.router)
app.include_router(sync_health_router.router)
