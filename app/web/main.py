"""FastAPI web application for EVE Retroindustry."""
from __future__ import annotations

from app.version import APP_VERSION  # single source of truth (app/version.py)
from app.web import security
from app.web.security import ensure_sessions_table

import asyncio
import datetime
import os
import json
import re
import sqlite3
import sys as _sys
import threading
import time as _time
import zipfile as _zipfile

import httpx
from app.esi.client import (
    esi_client, esi_error_message,
    set_market_token_provider as _esi_set_market_token_provider,
)
from fastapi import FastAPI, Request, Form
from fastapi.responses import (
    HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse,
)
from urllib.parse import quote

from app.auth.token_store import (
    ensure_characters_table,
    list_characters,
    has_any_character,
    get_character_row,
    get_valid_token as _get_valid_token_for,
    is_refresh_invalid,
    delete_character,
    update_corporation_id,
    update_last_sync,
)
from app.auth.esi_oauth import begin_login, complete_login, callback_url, LoginError
from app.character.blueprints import fetch_blueprints, ensure_bp_table
from app.character import wallet as wallet_api
from app.character import orders as orders_api
from app.character import jobs as jobs_api
from app.character import contracts as contracts_api
from app.character import planets as planets_api
from app.web import contracts_helper
from app.web import pi_planner_helper
from app.web import app_defaults
from app.market.taxes import selling_costs
from app.manufacturing import invention
from app.manufacturing.margins import build_invention_params
from app.web import margins_helper
from app.web import reactions_helper
from app.character.assets import (
    fetch_assets, ensure_assets_table, assets_at_location,
    fetch_corp_assets, ensure_corp_assets_table,
)
from app.db.type_resolver import resolve_names_bulk
from app.esi.client import search_type_by_name
from app.cache.blueprint_cache import resolve_type
from app.db.database import get_session
from app.manufacturing.planner import (
    build_plan, find_blueprint_for_product, calc_job_time, format_duration,
    MFG_IMPLANTS, MFG_IMPLANT_PCTS,
)
from app.bom.resolver import BOMResolver
from app.market.prices import ensure_price_table, fetch_station_volumes, get_cached_station_volumes, get_station_volumes_any_age, fetch_structure_market, TRADE_HUBS, JITA_REGION
from app.web.prices_helper import (
    get_prices_for_ids,
    get_cached_prices_for_ids,
    get_price_cache_stats,
    refresh_jita_prices_all,
    get_all_price_items,
    set_custom_price,
    stream_jita_refresh,
    stream_hub_refresh,
    get_hub_cache_stats,
    get_all_hub_prices,
    get_price_history,
)
from app.web.location_resolver import (
    resolve_station_names_bulk,
    ensure_location_name_table,
    load_location_names_from_db,
    locations_in_system,
    get_region_for_location,
    get_security_status,
)
from app.web.industry_helper import (
    ensure_industry_tables,
    get_adjusted_prices,
    get_sci_for_system,
    get_station_me_bonus,
    save_station_me_bonus,
    get_station_te_multiplier,
    get_station_me_bonus_pct,
    get_station_me_multiplier,
    get_station_facility,
    get_product_te_multiplier,
    get_station_cost_bonus,
    populate_rig_bonuses,
    get_rig_types,
    save_station_rigs_full,
    get_station_rigs_full,
    _SCC,
)
from app.character.skills import (
    ensure_skills_table,
    fetch_skills,
    fetch_skill_queue,
    fetch_location,
    fetch_ship,
    get_cached_skills,
    get_mfg_skill_ids,
)
from app.web.projects_helper import (
    ensure_project_tables,
    list_projects,
    create_project,
    add_plan_to_project,
    get_project_detail,
)
from app.db.migrate import upgrade_to_head
from app.db.schema import (
    ensure_schema as ensure_db_schema,
    ensure_sde_schema,
    forget_applied,
    sde_index_ddl,
)

from app.web.deps import (
    ACTIVE_COOKIE,
    DB_ABS,
    STATIC_DIR,
    TEMPLATES_DIR,
    _APP_DIR,
    _BUNDLE_DIR,
    _SDE_READY,
    _age_short,
    _container_display_name,
    _count_eu,
    _deny,
    _ensure_groups_populated,
    _format_date,
    _format_number,
    _isk,
    _isk0,
    _load_assets_from_cache,
    _load_blueprints_from_cache,
    _load_corp_assets_from_cache,
    _price_eu,
    _resolve_party_names,
    _tr,
    _ts_ago,
    _ts_to_str,
    _wants_html,
    ensure_schema,
    get_active_character,
    get_active_character_id,
    get_active_token,
    get_conn,
    get_token_for,
    templates,
)

# Tracks post-login ESI sync state. Everything past running/done exists so the
# loading screen can report REAL progress: it used to cycle three canned messages
# off the poll counter and sit on "Almost done…" indefinitely, which told a tester
# nothing about whether the app was still working.
_SYNC_STEPS: tuple[str, ...] = ("blueprints", "assets", "skills", "corp assets")

_sync_state: dict = {
    "running": False,
    "done": False,
    "total": 0,       # characters to sync
    "index": 0,       # 1-based character being fetched right now
    "char": "",       # its name
    "step": "",       # which of _SYNC_STEPS is in flight
    "phase": "",      # "characters" | "locations"
    "started_at": 0.0,
    "failed": 0,      # characters skipped or errored
}


def _sync_reset() -> None:
    _sync_state.update({
        "running": True, "done": False, "total": 0, "index": 0,
        "char": "", "step": "", "phase": "", "started_at": _time.time(), "failed": 0,
    })


def _sync_pct() -> int:
    """Honest completion estimate, 0–100.

    Characters share 92 % between them, split again across the four fetches per
    character, so the bar advances several times per character instead of jumping.
    The trailing station-name resolution owns the last few percent.
    """
    if _sync_state["done"]:
        return 100
    if _sync_state.get("phase") == "locations":
        return 96
    total = _sync_state.get("total") or 0
    if not total:
        return 2
    step = _sync_state.get("step") or ""
    step_i = _SYNC_STEPS.index(step) if step in _SYNC_STEPS else 0
    done_chars = max(0, (_sync_state.get("index") or 1) - 1)
    frac = (done_chars + step_i / len(_SYNC_STEPS)) / total
    return max(2, min(95, int(2 + 92 * frac)))

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
    from fastapi.responses import PlainTextResponse
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
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Bad Host header", status_code=400)

    path = request.url.path
    if security.is_public_path(path):
        return await call_next(request)

    conn = get_conn()
    try:
        session = security.load_session(conn, request.cookies.get(security.SESSION_COOKIE))
    finally:
        conn.close()

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

    try:
        conn = get_conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
        except sqlite3.OperationalError:
            count = 0

        _SDE_READY[0] = count > 0
        if _SDE_READY[0]:
            populate_rig_bonuses(conn)
            await _ensure_groups_populated(conn)
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


# ---------------------------------------------------------------------------
# First-run setup routes
# ---------------------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return _tr("setup.html", request, {"command": "python import_sde.py"})


# CCP issues client IDs as a 32-character hex string. Matched loosely (letters
# and digits, 20–64) rather than exactly, so a future change of format does not
# lock anyone out of their own setup page — the check is here to catch a pasted
# URL or secret key, not to validate CCP's ID scheme.
_PLAUSIBLE_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{20,64}")


def _first_run_setup_available(request: Request) -> bool:
    """Whether first-run client ID entry may be served at all.

    Two fences, because this route is deliberately sessionless — asking for a
    session would be circular, since the client ID is what makes logging in
    possible. It exists only while there is nothing configured to protect, and
    only on loopback, where the person reaching it is the person at the keyboard.
    A hosted deployment sets EVE_CLIENT_ID in its environment instead.
    """
    from app.auth.token_store import get_client_id
    if not security.host_is_loopback(request.headers.get("host")):
        return False
    return get_client_id() is None


@app.get("/setup/client-id", response_class=HTMLResponse)
async def setup_client_id_page(request: Request, error: str = ""):
    from app.auth.esi_oauth import callback_url, SCOPES
    if not _first_run_setup_available(request):
        # 404 rather than a redirect: once a client ID exists this route is not
        # "forbidden", it has nothing left to do, and saying so invites probing.
        return PlainTextResponse("Not found", status_code=404)
    return _tr("setup_client_id.html", request, {
        "callback_url": callback_url(),
        "scopes": SCOPES.split() if isinstance(SCOPES, str) else list(SCOPES),
        "error": error,
    })


@app.post("/setup/client-id")
async def setup_client_id_save(request: Request):
    from app.auth.token_store import save_client_id
    if not _first_run_setup_available(request):
        return PlainTextResponse("Not found", status_code=404)
    # Public paths skip the gate's CSRF check, so Origin is the guard here.
    if not security.origin_is_allowed(request.headers.get("origin")):
        return PlainTextResponse("Cross-site request rejected", status_code=400)

    form = await request.form()
    client_id = str(form.get("client_id") or "").strip()
    if not client_id:
        return RedirectResponse("/setup/client-id?error=Enter+the+Client+ID+from+your+EVE+application.",
                                status_code=303)
    if not _PLAUSIBLE_CLIENT_ID.fullmatch(client_id):
        # A wrong-shaped value fails much later, at CCP, with a message that does
        # not mention this form — so reject the obvious mistakes (a pasted URL, a
        # secret key, stray whitespace) while the user is still looking at it.
        return RedirectResponse(
            "/setup/client-id?error=That+does+not+look+like+a+Client+ID:+expect+about+32+"
            "letters+and+digits,+with+no+spaces+or+punctuation.", status_code=303)

    save_client_id(client_id)
    return RedirectResponse("/auth/login", status_code=303)


_WALLET_CACHE_TTL = 300.0  # 5 minutes


async def _fetch_wallet_balance(
    conn: sqlite3.Connection, char_id: int, token: str | None
) -> float | None:
    """Returns ISK wallet balance, using a 5-min SQLite cache."""
    now = _time.time()
    row = conn.execute(
        "SELECT balance, cached_at FROM char_wallet_cache WHERE character_id=?", (char_id,)
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
                    "INSERT INTO char_wallet_cache (character_id, balance, cached_at) VALUES (?,?,?) ON CONFLICT (character_id) DO UPDATE SET balance=excluded.balance, cached_at=excluded.cached_at",
                    (char_id, balance, now),
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
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT access_token FROM characters"
                " WHERE access_token IS NOT NULL AND token_expires_at > ?"
                " ORDER BY token_expires_at DESC LIMIT 1",
                (now + 30,),          # margin, so it cannot expire mid-flight
            ).fetchone()
            token = row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        token = None
    # Cached even when None: with nobody logged in this would otherwise hit the
    # DB on every single market request.
    _market_token_cache.update({"token": token, "until": now + 60})
    return token


_esi_set_market_token_provider(_market_bucket_token)


def _science_skill_mult(
    conn: sqlite3.Connection,
    bp_type_id: int,
    activity: str,
    skills: dict[int, int],
    preloaded: list[tuple[int, int]] | None = None,
) -> tuple[float, list[tuple[str, int, float, int]]]:
    """Return (multiplier, [(skill_name, char_level, bonus_pct, required_level), ...]).

    Each required skill with a time bonus contributes (1 - level * bonus_pct/100).
    Industry and AdvIndustry are handled separately — we skip them here.

    `preloaded`: [(skill_id, required_level), …] from the bulk fetch in plan_result.
    If passed, we avoid per-bp DB queries — we only look up names and bonus_pct
    from (process-level cached) lookup tables.
    """
    if preloaded is not None:
        # Fast path: bulk-prefetched in caller. Resolve names + bonus_pct
        # from small joined tables; cache them on the function for the rest
        # of the process — sde_skill_time_bonus has only ~27 rows.
        if not hasattr(_science_skill_mult, "_bonus_cache"):
            _science_skill_mult._bonus_cache = {  # type: ignore[attr-defined]
                r[0]: r[1] for r in conn.execute(
                    "SELECT skill_type_id, time_bonus_pct FROM sde_skill_time_bonus"
                ).fetchall()
            }
        if not hasattr(_science_skill_mult, "_name_cache"):
            _science_skill_mult._name_cache = {}  # type: ignore[attr-defined]
        bonus_cache = _science_skill_mult._bonus_cache  # type: ignore[attr-defined]
        name_cache: dict[int, str] = _science_skill_mult._name_cache  # type: ignore[attr-defined]
        # Lazily resolve names for skill_ids we haven't seen yet.
        missing = [sid for sid, _ in preloaded if sid not in name_cache]
        if missing:
            ph = ",".join("?" * len(missing))
            for sid, name in conn.execute(
                f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})",
                missing,
            ).fetchall():
                name_cache[sid] = name
        mult = 1.0
        details: list[tuple[str, int, float, int]] = []
        for sid, req_level in preloaded:
            level = skills.get(sid, 0)
            bonus_pct = bonus_cache.get(sid)
            if bonus_pct is not None:
                mult *= 1.0 - level * bonus_pct / 100
            details.append(
                (name_cache.get(sid, f"Skill {sid}"), level,
                 float(bonus_pct or 0), int(req_level))
            )
        return max(0.01, mult), details

    # Slow path — preloaded not available (single-blueprint callers).
    try:
        rows = conn.execute(
            """SELECT bs.skill_type_id,
                      COALESCE(st.skill_name, t.name) AS skill_name,
                      bs.required_level,
                      st.time_bonus_pct
               FROM sde_blueprint_skills bs
               LEFT JOIN sde_skill_time_bonus st ON st.skill_type_id = bs.skill_type_id
               LEFT JOIN sde_types t              ON t.type_id       = bs.skill_type_id
               WHERE bs.blueprint_type_id = ? AND bs.activity = ?
                 AND bs.skill_type_id NOT IN (3380, 3388)""",
            (bp_type_id, activity),
        ).fetchall()
    except Exception:
        return 1.0, []

    mult = 1.0
    details: list[tuple[str, int, float, int]] = []
    for skill_id, skill_name, req_level, bonus_pct in rows:
        level = skills.get(skill_id, 0)
        if bonus_pct is not None:
            mult *= 1.0 - level * bonus_pct / 100
        details.append((skill_name or f"Skill {skill_id}", level, float(bonus_pct or 0), int(req_level)))
    return max(0.01, mult), details


def _collect_type_ids(node) -> list[int]:
    ids = [node.type_id]
    for child in node.children:
        ids.extend(_collect_type_ids(child))
    return ids


def _is_real_location(loc_id: int) -> bool:
    """Return True if the ID is a real station/structure, not a container/ship item_id."""
    # NPC stations: 60_000_000 – 64_000_000
    # Player structures: > 1_000_000_000_000
    # Solar systems: 30_000_000 – 34_000_000 (things in space)
    # Ship/container item_ids: typically billions but < 1 trillion
    if 60_000_000 <= loc_id < 64_000_000:
        return True
    if loc_id > 1_000_000_000_000:
        return True
    if 30_000_000 <= loc_id < 34_000_000:
        return True  # solar system — things in space
    return False


def _resolve_root_locations(assets: list) -> dict[int, int]:
    """
    Return {item_id: root_location_id} where root_location_id is a real station/structure.
    Walks the chain item_id → location_id until it reaches a real location.
    """
    # Map item_id → location_id for fast parent lookup
    parent: dict[int, int] = {a.item_id: a.location_id for a in assets}

    result: dict[int, int] = {}
    for a in assets:
        loc = a.location_id
        seen: set[int] = set()
        while not _is_real_location(loc) and loc in parent and loc not in seen:
            seen.add(loc)
            loc = parent[loc]
        result[a.item_id] = loc
    return result


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/auth/login")
async def auth_login(request: Request):
    """Send the browser to EVE SSO.

    A plain redirect. The desktop build opened SSO in the system browser and
    parked the webview on a waiting page that polled for completion, because the
    redirect came back to a separate local server. It now comes back to
    /callback on this app, so there is nothing to wait for.
    """
    _sync_state["done"] = False
    url = begin_login()
    if not url:
        # No client ID, and the callback is not localhost — so this deployment has
        # not registered its own EVE application. Starting the flow anyway would
        # fail at the token exchange with a far less obvious message.
        # On localhost the fix is a form, not a restart — send them straight to it.
        if _first_run_setup_available(request):
            return RedirectResponse("/setup/client-id", status_code=303)
        return _tr("auth_failed.html", request, {
            "reason": "This deployment has no EVE application configured.",
            # Drives the recovery steps in the template. The Settings button is
            # hidden in this state on purpose: /settings needs a session, a
            # session needs SSO, and SSO is the thing that cannot start — so the
            # button silently round-trips back to this page and reads as broken.
            "unconfigured": True,
            "callback_url": callback_url(),
        })
    return RedirectResponse(url, status_code=303)


@app.get("/callback")
async def auth_callback(request: Request, code: str | None = None,
                        state: str | None = None):
    """Where EVE sends the browser back. Turns the code into a session.

    Public by necessity — the caller cannot have a session yet. What makes that
    safe is the state check inside complete_login(): the callback must carry a
    state this server issued and has not already spent, or there is no PKCE
    verifier to exchange with and the flow stops here.
    """
    # Snapshot who was already trusted, BEFORE the exchange. complete_login()
    # stores the character as part of completing it, so afterwards there is no
    # way to tell a first-time arrival from a re-authentication of someone
    # already added — and that difference decides whether a refusal is allowed
    # to delete the row. Getting it wrong would destroy a known alt whose owner's
    # session merely expired during the round trip to EVE.
    conn = get_conn()
    try:
        known_before = {cid for cid, _name in list_characters(conn)}
    finally:
        conn.close()

    try:
        character_id, character_name = complete_login(code, state)
    except LoginError as exc:
        return _tr("auth_failed.html", request, {"reason": str(exc)})

    conn = get_conn()
    try:
        if not security.may_sign_in(conn, character_id):
            # Not necessarily a failure. "Add Character" and "Log In" are the same
            # link, so intent is not recorded anywhere — but complete_login() has
            # already stored this character's tokens, which means the *character*
            # is added and only the *session* is being refused. An owner who is
            # signed in and adding an alt is the ordinary case, and telling them
            # "Login failed. Nothing was changed." is wrong twice over: nothing
            # failed, and something did change. Whoever already holds a session is
            # the owner doing exactly that; a stranger who found the URL does not.
            if security.load_session(conn, request.cookies.get(security.SESSION_COOKIE)):
                print(f"[auth] added character {character_id} ({character_name}) "
                      f"to the instance owned by {security.get_owner_id(conn)}", flush=True)
                return RedirectResponse("/auth/sync", status_code=303)
            owner = security.get_owner_id(conn)
            print(f"[auth] refused session for character {character_id}: this "
                  f"instance belongs to {owner}", flush=True)
            # Refused, and nobody here vouched for them — so do not keep the
            # refresh token complete_login() just wrote. Holding a stranger's ESI
            # credential, obtained because they found the URL, is custody without
            # consent, policy or a revocation path (§13 R4). Only a character this
            # login created is removed: one that was already known is a re-auth by
            # someone previously added, and deleting it would lose real data.
            if character_id not in known_before:
                delete_character(conn, character_id)
                print(f"[auth] discarded the tokens just stored for uninvited "
                      f"character {character_id}", flush=True)
            return _tr("auth_failed.html", request, {
                "reason": f"{character_name} is not the owner of this instance.",
            })
        session_id, _ = security.create_session(conn, character_id)
    finally:
        conn.close()

    resp = RedirectResponse("/auth/sync", status_code=303)
    security.set_session_cookie(resp, session_id)
    return resp


async def _bg_initial_sync():
    """Fetch blueprints + personal + corp assets from ESI for every known char."""
    conn = None
    try:
        conn = get_conn()
        chars = list_characters(conn)
        if not chars:
            return

        all_loc_ids: set[int] = set()
        any_token: str | None = None
        _sync_state["total"] = len(chars)
        _sync_state["phase"] = "characters"

        async with esi_client() as client:
            for _n, (char_id, _name) in enumerate(chars, start=1):
                # Published before each fetch so the loading screen can name the
                # character and the step it is waiting on, which is the difference
                # between "slow" and "stuck" for whoever is watching.
                _sync_state.update({"index": _n, "char": _name, "step": ""})
                try:
                    # Off the event loop + serialized with the dashboard's token
                    # fetch (shared per-char refresh lock) so the two can't
                    # invalidate each other's rotating refresh token.
                    token = await _valid_token_async(char_id)
                except Exception as exc:
                    print(f"[sync] token refresh failed for {char_id}: {exc}", flush=True)
                    _sync_state["failed"] += 1
                    continue
                if not token:
                    _sync_state["failed"] += 1
                    continue
                any_token = token
                try:
                    _sync_state["step"] = "blueprints"
                    await fetch_blueprints(client, char_id, token, conn)
                    _sync_state["step"] = "assets"
                    personal = await fetch_assets(client, char_id, token, conn)
                    _sync_state["step"] = "skills"
                    await fetch_skills(client, char_id, token, conn)
                    _sync_state["step"] = "corp assets"
                    try:
                        corp_id, corp = await fetch_corp_assets(client, char_id, token, conn)
                        if corp_id:
                            update_corporation_id(conn, char_id, corp_id)
                    except Exception as exc:
                        print(f"[sync] corp_assets failed for {char_id}: {exc}", flush=True)
                        corp = []
                    all_loc_ids |= {a.location_id for a in personal}
                    all_loc_ids |= {a.location_id for a in corp}
                    update_last_sync(conn, char_id)
                except Exception as exc:
                    print(f"[sync] character {char_id} sync failed: {exc}", flush=True)
                    _sync_state["failed"] += 1
                    continue

        if all_loc_ids and any_token:
            _sync_state.update({"phase": "locations", "step": "", "char": ""})
            try:
                await resolve_station_names_bulk(list(all_loc_ids), token=any_token, conn=conn)
            except Exception as exc:
                print(f"[sync] resolve_station_names_bulk failed: {exc}", flush=True)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[sync] fatal: {exc}", flush=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _sync_state["running"] = False
        _sync_state["done"] = True

@app.get("/auth/sync", response_class=HTMLResponse)
async def auth_sync(request: Request):
    """Landing page after a successful login. Kicks off the first ESI sync.

    Authenticated like everything else — /callback has already set the cookie by
    the time the browser arrives here.
    """
    conn = get_conn()
    try:
        if not has_any_character(conn):
            return RedirectResponse("/")
    finally:
        conn.close()

    if not _sync_state["running"] and not _sync_state["done"]:
        _sync_reset()
        asyncio.create_task(_bg_initial_sync())
    return _tr("sync.html", request, {})


@app.get("/auth/bootstrap")
async def auth_bootstrap(request: Request, token: str | None = None):
    """Redeem a token from `python -m app.web.bootstrap` for a session.

    The way back in when SSO cannot be used — during the minutes when the
    callback URL registered with CCP does not yet match this deployment. Minting
    a token needs filesystem access to the database, so this route hands nothing
    to anyone who does not already have the server.
    """
    from app.web.bootstrap import redeem_token

    conn = get_conn()
    try:
        character_id = redeem_token(conn, token)
        if character_id is None:
            return _tr("auth_failed.html", request, {
                "reason": "That sign-in link has already been used, or has expired.",
            })
        if security.get_owner_id(conn) is None:
            security.claim_owner(conn, character_id)
        session_id, _ = security.create_session(conn, character_id)
    finally:
        conn.close()

    print(f"[auth] bootstrap session issued for character {character_id}", flush=True)
    resp = RedirectResponse("/", status_code=303)
    security.set_session_cookie(resp, session_id)
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """Drop this session. The character's ESI tokens are untouched."""
    from fastapi.responses import JSONResponse

    conn = get_conn()
    try:
        security.delete_session(conn, request.cookies.get(security.SESSION_COOKIE))
    finally:
        conn.close()
    resp = JSONResponse({"ok": True})
    security.clear_session_cookie(resp)
    return resp


@app.get("/api/sync-status")
async def api_sync_status():
    started = _sync_state.get("started_at") or 0.0
    return {
        "done":    _sync_state["done"],
        "running": _sync_state["running"],
        "pct":     _sync_pct(),
        "total":   _sync_state.get("total") or 0,
        "index":   _sync_state.get("index") or 0,
        "char":    _sync_state.get("char") or "",
        "step":    _sync_state.get("step") or "",
        "phase":   _sync_state.get("phase") or "",
        "failed":  _sync_state.get("failed") or 0,
        "elapsed": int(_time.time() - started) if started else 0,
    }


# ---------------------------------------------------------------------------
# Multi-character management endpoints
# ---------------------------------------------------------------------------

@app.post("/api/characters/{char_id}/activate")
async def api_activate_character(char_id: int):
    """Set active_char cookie."""
    from fastapi.responses import JSONResponse
    conn = get_conn()
    try:
        if not get_character_row(conn, char_id):
            return JSONResponse({"ok": False, "error": "Unknown character"}, status_code=404)
    finally:
        conn.close()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(ACTIVE_COOKIE, str(char_id), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.delete("/api/characters/{char_id}")
async def api_delete_character(request: Request, char_id: int):
    """Remove a character (and its cached data)."""
    from fastapi.responses import JSONResponse
    conn = get_conn()
    try:
        delete_character(conn, char_id)
    finally:
        conn.close()
    resp = JSONResponse({"ok": True})
    if request.cookies.get(ACTIVE_COOKIE) == str(char_id):
        resp.delete_cookie(ACTIVE_COOKIE)
    return resp


@app.post("/api/sync/start")
async def api_sync_start():
    """Manually trigger an ESI sync for all characters."""
    if _sync_state["running"]:
        return {"ok": False, "error": "Already running"}
    _sync_reset()
    asyncio.create_task(_bg_initial_sync())
    return {"ok": True}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.auth.token_store import get_client_id
    from app.auth.esi_oauth import callback_url, SCOPES
    conn = get_conn()
    try:
        defaults = app_defaults.get_defaults(conn)
        station_options = _industry_station_options(conn)
        # Only the eight canonical decryptors: the faction-flavoured duplicates
        # and the ancient-relic ones behave identically or belong to reverse
        # engineering, and 64 entries would make the picker unusable.
        decryptors = [d for d in invention.list_decryptors(conn)
                      if d.name.endswith("Decryptor")]
    finally:
        conn.close()
    return _tr("settings.html", request, {
        "client_id": get_client_id() or "",
        "callback_url": callback_url(),
        "trade_hubs": TRADE_HUBS,
        "scopes": SCOPES,
        "defaults": defaults,
        "station_options": station_options,
        "decryptors": decryptors,
    })


def _industry_station_options(conn: sqlite3.Connection) -> list[dict]:
    """Stations we know a name for — the candidates for a build/reaction default.

    Sourced from the shared location-name cache, so it lists exactly the places
    this install has already seen (assets, jobs, a previous /plan).
    """
    try:
        rows = conn.execute(
            "SELECT location_id, name FROM location_name_cache "
            "WHERE name IS NOT NULL AND name <> '' ORDER BY name"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"location_id": r[0], "name": r[1]} for r in rows]


@app.post("/api/settings/defaults")
async def api_save_defaults(request: Request):
    """Saves the app-wide industry defaults used by the margin tracker and
    pre-filled into the /plan form."""
    body = await request.json()
    conn = get_conn()
    try:
        saved = app_defaults.save_defaults(conn, body)
    finally:
        conn.close()
    return {"ok": True, "defaults": saved}


@app.post("/api/settings/client-id")
async def api_save_client_id(request: Request):
    body = await request.json()
    cid = body.get("client_id", "").strip()
    if not cid:
        return {"ok": False, "error": "Client ID cannot be empty."}
    from app.auth.token_store import save_client_id
    save_client_id(cid)
    return {"ok": True}


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


async def _valid_token_async(char_id: int) -> str | None:
    """Fetch (refreshing if expired) a character's access token WITHOUT blocking
    the event loop. get_valid_token() does a synchronous httpx.post on expiry;
    calling it inline on the async loop froze the whole app. Run it in a worker
    thread with its own SQLite connection (sqlite objects are single-thread)."""
    def _work() -> str | None:
        c = get_conn()
        try:
            return _get_valid_token_for(c, char_id)
        finally:
            try:
                c.close()
            except Exception:
                pass
    return await asyncio.to_thread(_work)


async def _compute_dashboard(request: Request, conn, *, live: bool) -> dict:
    """Build the dashboard context.

    live=False → everything from the local cache/DB, NO ESI call, renders
    instantly. live=True → also fetch the ESI-backed fields (corp/system names,
    wallet, location, skill queue, adjusted prices); used by /api/dashboard/live.
    Splitting it this way means a slow or rate-limited ESI can never make the
    dashboard (or the whole app) appear frozen.
    """
    logged_in = has_any_character(conn)
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

    chars = list_characters(conn)
    active_char_id = get_active_character_id(request, conn)
    char_rows: dict[int, dict] = {cid: (get_character_row(conn, cid) or {}) for cid, _ in chars}

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
        raw = _load_assets_from_cache(conn, cid)
        all_assets_by_char[cid] = raw
        assets_by_char[cid] = [a for a in raw if not a.get("is_singleton", False)]
        all_type_ids_set.update(a["type_id"] for a in raw)

    # Prices: full (ESI adjusted for missing) when live, cache-only otherwise.
    prices: dict[int, tuple] = {}
    if all_type_ids_set:
        if live:
            prices = await get_prices_for_ids(conn, list(all_type_ids_set))
        else:
            prices = get_cached_prices_for_ids(conn, list(all_type_ids_set))

    # Blueprint group_ids — exclude from net worth (matches in-game behavior)
    bp_group_ids: set[int] = {
        r[0] for r in conn.execute(
            "SELECT group_id FROM sde_groups WHERE name LIKE '%Blueprint%'"
        ).fetchall()
    }
    type_group: dict[int, int] = {
        r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, group_id FROM sde_types WHERE type_id IN ({','.join('?' * len(all_type_ids_set))})",
            list(all_type_ids_set),
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
                dock_names = await resolve_station_names_bulk(list(_dock_ids), token=_any_tok, conn=conn)
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
            _ph = ",".join("?" * len(_skill_ids))
            skill_names = {r[0]: r[1] for r in conn.execute(
                f"SELECT type_id, name FROM sde_types WHERE type_id IN ({_ph})", list(_skill_ids)
            ).fetchall()}

    # Per-character cards.
    for cid, cname in chars:
        char_row = char_rows[cid]
        bp_row = conn.execute(
            "SELECT json_array_length(data_json) FROM char_blueprints_cache WHERE character_id=?",
            (cid,),
        ).fetchone()
        bp_count = bp_row[0] if bp_row and bp_row[0] else 0

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
                "SELECT name FROM sde_types WHERE type_id=?", (_ship_type,)
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

    price_stats = get_price_cache_stats(conn)

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
    conn = get_conn()
    try:
        ctx = await _compute_dashboard(request, conn, live=False)
    finally:
        conn.close()
    ctx["login_busy"] = request.query_params.get("login_busy") == "1"
    return _tr("index.html", request, ctx)


@app.get("/api/dashboard/live")
async def api_dashboard_live(request: Request):
    """ESI-backed dashboard data (corp names, wallet, location, skill queue,
    refined prices), fetched by the dashboard right after the instant render."""
    conn = get_conn()
    try:
        ctx = await _compute_dashboard(request, conn, live=True)
    finally:
        conn.close()
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


# ---------------------------------------------------------------------------
# Manufacturing plan
# ---------------------------------------------------------------------------

@app.get("/plan", response_class=HTMLResponse)
async def plan_form(request: Request, char: str = "", station: str = ""):
    conn = get_conn()
    # Determine which character drives the form (URL ?char= overrides active cookie)
    plan_char_id: int | None = None
    if char.isdigit():
        plan_char_id = int(char)
        if not get_character_row(conn, plan_char_id):
            plan_char_id = None
    if plan_char_id is None:
        plan_char_id = get_active_character_id(request, conn)
    char_row = get_character_row(conn, plan_char_id) if plan_char_id else None
    token = _get_valid_token_for(conn, plan_char_id) if plan_char_id else None

    location_ids = []
    char_skills: dict[int, int] = {}
    if char_row:
        raw = _load_assets_from_cache(conn, char_row["character_id"])
        location_ids = sorted({a["location_id"] for a in raw if not a.get("is_singleton", False)})
        if token:
            async with esi_client() as client:
                char_skills = await fetch_skills(client, char_row["character_id"], token, conn)
        else:
            char_skills = get_cached_skills(conn, char_row["character_id"])
    product_param = request.query_params.get("product", "")
    if product_param.strip().isdigit():
        row = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (int(product_param),)).fetchone()
        if row:
            product_param = row[0]
    # Preserve station when switching character; otherwise fall back to the
    # app-wide default from Settings so the form opens ready to run.
    prefill_station = station.strip() if station.strip().isdigit() else ""
    _defaults = app_defaults.get_defaults(conn)
    if not prefill_station and _defaults.get("build_station_id"):
        prefill_station = str(_defaults["build_station_id"])
    prefill_station_name = ""
    if prefill_station:
        row = conn.execute(
            "SELECT name FROM location_name_cache WHERE location_id=?", (int(prefill_station),)
        ).fetchone()
        if row:
            prefill_station_name = row[0]
    stock_default = int(prefill_station) if prefill_station else 0
    stock_station_options = await _build_stock_station_options(
        conn, plan_char_id, token,
        selected_ids=set(), default_station=stock_default, explicit=False,
    )
    conn.close()
    return _tr("plan.html", request, {
        "locations": location_ids,
        "stock_station_options": stock_station_options,
        "form_stock_stations": "",
        "result": None,
        "error": None,
        "form_product": product_param,
        "form_station": prefill_station,
        "form_station_name": prefill_station_name,
        "form_industry":     str(char_skills.get(3380, 0)),
        "form_adv_industry": str(char_skills.get(3388, 0)),
        "form_implant_mfg":  "0",
        "mfg_implant_options": _MFG_IMPLANT_OPTIONS,
        "plan_char_id": plan_char_id,
        "form_facility_tax": str(_defaults.get("facility_tax", 2.5)),
        "form_reaction_station": str(_defaults.get("reaction_station_id") or ""),
    })


def _resolve_product_local(conn: sqlite3.Connection, query: str) -> tuple[int, str] | None:
    """Find a product's type_id by name in the local SDE.

    Strategy: exact → prefix → substring. Among candidates it prefers
    producible ones (have a manufacturing/reaction recipe), then published,
    then the shortest name. That way "Industrial Jump Portal Generator" hits
    "…Generator I" instead of its blueprint or a longer variant.
    Returns None if nothing matches.
    """
    q = query.strip()
    if not q:
        return None

    def _pick(rows: list[tuple]) -> tuple[int, str] | None:
        if not rows:
            return None
        producible = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT product_type_id FROM sde_blueprint_products"
                " WHERE activity IN ('manufacturing','reaction')"
                f"   AND product_type_id IN ({','.join('?' * len(rows))})",
                [r[0] for r in rows],
            ).fetchall()
        }
        # best = producible > published > shorter name > lower type_id
        rows = sorted(rows, key=lambda r: (
            0 if r[0] in producible else 1,
            0 if r[2] else 1,
            len(r[1]),
            r[0],
        ))
        return rows[0][0], rows[0][1]

    # 1) exact
    exact = conn.execute(
        "SELECT type_id, name, published FROM sde_types WHERE name = ? COLLATE NOCASE",
        (q,),
    ).fetchall()
    hit = _pick(exact)
    if hit:
        return hit
    # 2) prefix (limited so it doesn't explode on generic words)
    pref = conn.execute(
        "SELECT type_id, name, published FROM sde_types"
        " WHERE name LIKE ? COLLATE NOCASE LIMIT 200",
        (q + "%",),
    ).fetchall()
    hit = _pick(pref)
    if hit:
        return hit
    # 3) substring
    sub = conn.execute(
        "SELECT type_id, name, published FROM sde_types"
        " WHERE name LIKE ? COLLATE NOCASE LIMIT 200",
        ("%" + q + "%",),
    ).fetchall()
    return _pick(sub)


# Dropdown entries for the manufacturing implant, cheapest bonus first. Built from
# planner.MFG_IMPLANTS so the UI can never drift from what calc_job_time accepts.
_MFG_IMPLANT_OPTIONS: list[dict] = [
    {"pct": f"{pct:g}", "label": f"{name} (−{pct:g}%)"}
    for _tid, (name, pct) in sorted(MFG_IMPLANTS.items(), key=lambda kv: kv[1][1])
]


def _implant_name_for_pct(pct: float) -> str | None:
    """Name of the BX implant matching a reduction, or None when no implant."""
    for name, p in MFG_IMPLANTS.values():
        if p == pct:
            return name
    return None


@app.post("/plan", response_class=HTMLResponse)
async def plan_result(
    request: Request,
    product: str = Form(...),
    station: str = Form(""),
    reaction_station: int = Form(0),
    qty: int = Form(1),
    mode: str = Form("full"),
    form_me: str = Form(""),
    form_te: str = Form(""),
    facility_tax: str = Form("2.5"),
    reaction_facility_tax: str = Form(""),
    facility_me_bonus: str = Form("0"),
    reaction_me_bonus: str = Form("0"),
    selling_station: int = Form(0),
    form_industry: str = Form("0"),
    form_adv_industry: str = Form("0"),
    plan_char_id: str = Form(""),
    runs_per_job: str = Form("1"),
    stock_stations: str = Form(""),
    input_basis: str = Form("sell"),
    implant_mfg: str = Form("0"),
):
    conn = get_conn()
    input_basis = "buy" if input_basis == "buy" else "sell"
    # Zainou 'Beancounter' Industry implant — the UI sends the reduction in %.
    # Anything not in the real BX-80x set collapses to 0 (no implant).
    try:
        implant_mfg_pct = float(implant_mfg.replace(",", "."))
    except (ValueError, AttributeError):
        implant_mfg_pct = 0.0
    if implant_mfg_pct not in MFG_IMPLANT_PCTS:
        implant_mfg_pct = 0.0
    implant_mfg = f"{implant_mfg_pct:g}"
    error = None
    plan_data = None
    # Selection of stations the stock level is computed from. Empty = default
    # to the manufacturing station (backwards compatible). CSV location IDs from checkboxes.
    stock_station_ids: set[int] = {
        int(x) for x in stock_stations.split(",") if x.strip().lstrip("-").isdigit()
    }
    stock_explicit = bool(stock_stations.strip())
    # How many runs one BPC copy has — ME is rounded per job.
    # 1 (default) = parallel 1-run copies; K = copies of K runs each;
    # empty/0 = one batched job (in-game multi-run window).
    rpj_int: int | None = None
    if runs_per_job.strip().isdigit() and int(runs_per_job.strip()) > 0:
        rpj_int = int(runs_per_job.strip())
    # Resolve plan character from form, fall back to active char.
    plan_char_id_int: int | None = None
    if plan_char_id.strip().isdigit():
        candidate = int(plan_char_id.strip())
        if get_character_row(conn, candidate):
            plan_char_id_int = candidate
    if plan_char_id_int is None:
        plan_char_id_int = get_active_character_id(request, conn)

    # Parse station — friendly error instead of 422 (if missing, raise ValueError below)
    try:
        station = int(station.strip()) if isinstance(station, str) and station.strip() else 0
    except ValueError:
        station = 0

    # Convert ME/TE to int if provided
    me_override: int | None = int(form_me) if form_me.strip().isdigit() else None
    te_override: int | None = int(form_te) if form_te.strip().isdigit() else None
    # Safe defaults — overwritten inside try block once BP is known
    me: float = float(me_override) if me_override is not None else 0.0
    te: int   = te_override if te_override is not None else 0

    def _clamp_skill(s: str, max_val: int = 5) -> int:
        try:
            return max(0, min(max_val, int(s.strip())))
        except (ValueError, AttributeError):
            return 0

    industry_level     = _clamp_skill(form_industry)
    adv_industry_level = _clamp_skill(form_adv_industry)

    def _parse_pct(s: str) -> float:
        try:
            return max(0.0, min(25.0, float(s.replace(",", "."))))
        except (ValueError, AttributeError):
            return 0.0

    # facility_me_bonus / reaction_me_bonus from the form are now display-only
    # (form_facility_me_bonus is passed back to the template). The actual ME
    # multiplier is computed from station_rigs in get_station_me_multiplier.

    try:
        if plan_char_id_int is None:
            raise ValueError("You are not signed in.")
        token = _get_valid_token_for(conn, plan_char_id_int)
        row = get_character_row(conn, plan_char_id_int)
        if not token or not row:
            raise ValueError("You are not signed in.")
        if not station:
            raise ValueError("Select a manufacturing station.")
        char = (row["character_id"], row["character_name"])
        char_id, _ = char

        async with esi_client() as client:
            session = get_session()
            if product.strip().isdigit():
                type_id = int(product.strip())
                type_name = await resolve_type(client, session, type_id)
            else:
                # Local SDE resolve — exact → prefix → substring; prefers
                # producible, published, shortest name (so "Industrial
                # Jump Portal Generator" hits "…Generator I", not its
                # blueprint). ESI /universe/ids/ only as a last resort
                # (and it is purely exact-match).
                local = _resolve_product_local(conn, product.strip())
                if local:
                    type_id, type_name = local
                else:
                    results = await search_type_by_name(client, product.strip())
                    if not results:
                        raise ValueError(f"Product '{product}' not found.")
                    type_id = results[0]
                    type_name = await resolve_type(client, session, type_id)
            session.close()

        async with esi_client() as client:
            blueprints, all_assets, char_skills = await asyncio.gather(
                fetch_blueprints(client, char_id, token, conn),
                fetch_assets(client, char_id, token, conn),
                fetch_skills(client, char_id, token, conn),
            )

        # Industry/AdvIndustry always from current char_skills (the form_industry
        # field is hidden and may come from an old character after switching).
        industry_level     = max(0, min(5, int(char_skills.get(3380, 0))))
        adv_industry_level = max(0, min(5, int(char_skills.get(3388, 0))))
        form_industry      = str(industry_level)
        form_adv_industry  = str(adv_industry_level)

        # Stock sources: if the user picked stations, use them; otherwise default
        # to the manufacturing station. Roll up containers to their station +
        # exclude ship cargo/fittings via _rollup_stock (so selecting a station
        # also counts container contents, but not a ship's fit/cargo).
        effective_stock_ids = stock_station_ids if stock_explicit else {station}
        _station_types = _rollup_stock_from_charassets(all_assets)
        available = {}
        for sid in effective_stock_ids:
            for tid, q in _station_types.get(sid, {}).items():
                available[tid] = available.get(tid, 0) + q

        bp = find_blueprint_for_product(blueprints, type_id, conn)
        me = float(me_override if me_override is not None else (bp.material_efficiency if bp else 0))
        te = int(te_override if te_override is not None else (bp.time_efficiency if bp else 0))

        # Station ME multiplier — per-product (a rig applies only to products
        # matching its category: Ship rig to ships, Equipment rig to modules, etc.).
        eff_rxn_station_for_me = reaction_station if reaction_station else station
        mfg_facility = get_station_facility(conn, station)
        rxn_facility = get_station_facility(conn, eff_rxn_station_for_me)
        # Aggregated savings for the ROOT product (for display)
        mfg_me_mult = get_station_me_multiplier(conn, station)
        rxn_me_mult = get_station_me_multiplier(conn, eff_rxn_station_for_me)

        # === Manufacturing fee parameters, computed up-front ===
        # The make-vs-buy optimizer needs each job's install fee, not just its
        # material cost — otherwise it "makes" components whose real install
        # fees then quietly erase the paper savings. So resolve fee inputs
        # (SCI, tax, structure bonus, adjusted prices) BEFORE building the plan.
        def _safe_pct(s: str, default: float) -> float:
            try:
                return float(s.replace(",", "."))
            except (ValueError, AttributeError):
                return default

        fac_tax_pct  = _safe_pct(facility_tax, 2.5)
        fac_tax_rate = fac_tax_pct / 100

        # Reaction station — 0 means use the same one as manufacturing
        eff_rxn_station = reaction_station if reaction_station else station
        sep_rxn_station = eff_rxn_station != station

        rxn_fac_tax_pct  = _safe_pct(reaction_facility_tax, fac_tax_pct) if reaction_facility_tax.strip() else fac_tax_pct
        rxn_fac_tax_rate = rxn_fac_tax_pct / 100

        # Solar system ID of the manufacturing station
        sys_row = conn.execute(
            "SELECT solar_system_id FROM location_name_cache WHERE location_id=?", (station,)
        ).fetchone()
        solar_system_id: int | None = sys_row[0] if sys_row and sys_row[0] else None

        # Solar system ID of the reaction station
        if sep_rxn_station:
            rxn_sys_row = conn.execute(
                "SELECT solar_system_id FROM location_name_cache WHERE location_id=?", (eff_rxn_station,)
            ).fetchone()
            rxn_solar_system_id: int | None = rxn_sys_row[0] if rxn_sys_row and rxn_sys_row[0] else None
        else:
            rxn_solar_system_id = solar_system_id

        adj_prices = await get_adjusted_prices(conn)

        mfg_sci = await get_sci_for_system(conn, solar_system_id, "manufacturing") if solar_system_id else 0.0
        rxn_sci = await get_sci_for_system(conn, rxn_solar_system_id, "reaction") if rxn_solar_system_id else 0.0

        # TE multipliers for the stations (structure + rigs)
        mfg_te_mult = get_station_te_multiplier(conn, station)
        rxn_te_mult = get_station_te_multiplier(conn, eff_rxn_station) if sep_rxn_station else mfg_te_mult

        # Cost bonus na SCI (Raitaru −3 %, Azbel −4 %, Sotiyo −5 %)
        mfg_cost_bonus = get_station_cost_bonus(conn, station)
        rxn_cost_bonus = get_station_cost_bonus(conn, eff_rxn_station) if sep_rxn_station else mfg_cost_bonus

        # Combined install-fee rate per activity: SCI×(1−structure bonus) + tax + SCC.
        rate_mfg = mfg_sci * (1.0 - mfg_cost_bonus) + fac_tax_rate + _SCC
        rate_rxn = rxn_sci * (1.0 - rxn_cost_bonus) + rxn_fac_tax_rate + _SCC

        # Pass 1 — a structural resolve purely to discover which products the
        # tree contains, so their per-run times can be turned into per-product
        # job splits. Skipped entirely when splitting is off (the default), so
        # an unconfigured install pays nothing for it.
        _max_job_days = float(app_defaults.get_defaults(conn).get("max_job_days") or 0)
        _job_splits: dict[int, int] = {}
        if _max_job_days > 0:
            probe = BOMResolver(DB_ABS, blueprints=blueprints, runs_per_job=rpj_int)
            try:
                _job_splits = _derive_job_splits(
                    conn,
                    probe.resolve(type_id, qty, me=me, mfg_facility=mfg_facility,
                                  rxn_facility=rxn_facility),
                    max_days=_max_job_days, te=te,
                    industry_level=industry_level, adv_industry_level=adv_industry_level,
                    mfg_facility=mfg_facility, rxn_facility=rxn_facility,
                    char_skills=char_skills,
                )
            finally:
                probe.close()

        # Invention: a T2 item needs an invented BPC, and until v0.9.29 this page
        # charged nothing for it — not on nested components and not even on the
        # product itself, since only the margin tracker ever called that code.
        # Same builder the tracker uses, so the two pages price datacores alike.
        inv_params, inv_warnings = build_invention_params(
            conn, app_defaults.get_defaults(conn), input_basis)

        # Pass 2 — the real resolve, with the splits applied. ME rounds once per
        # job, so the splits reach the material totals here and in build_plan
        # below; both resolutions must use them or the two views disagree.
        # The resolver gets all of the character's blueprints → per-product ME is
        # looked up for each intermediate step (Capital Armor Plates ME may differ from root ME).
        resolver = BOMResolver(DB_ABS, blueprints=blueprints, runs_per_job=rpj_int,
                               adjusted_prices=adj_prices, rate_mfg=rate_mfg, rate_rxn=rate_rxn,
                               runs_per_job_by_product=_job_splits,
                               invention=inv_params)
        root = resolver.resolve(type_id, qty, me=me,
                                mfg_facility=mfg_facility,
                                rxn_facility=rxn_facility)
        resolver.close()

        all_ids = list(set(_collect_type_ids(root) + [type_id]))
        prices = await get_prices_for_ids(conn, all_ids)

        plan = build_plan(
            product_type_id=type_id,
            quantity=qty,
            location_id=station,
            available_assets=available,
            blueprints=blueprints,
            db_path=DB_ABS,
            mode=mode,
            prices=prices,
            mfg_facility=mfg_facility,
            rxn_facility=rxn_facility,
            runs_per_job=rpj_int,
            # Same splits as the steps resolution above — the Materials tab and
            # the Jobs list are two separate BOM resolutions and must not disagree.
            runs_per_job_by_product=_job_splits,
            adjusted_prices=adj_prices,
            rate_mfg=rate_mfg,
            rate_rxn=rate_rxn,
            input_basis=input_basis,
            # Hand over the SAME root ME/TE used for `root` above, so the Materials
            # tab and the Manufacturing steps cannot disagree (they are two separate
            # BOM resolutions of the same product).
            me_override=me,
            te_override=te,
            invention=inv_params,
        )
        plan.invention_unpriced = inv_warnings + plan.invention_unpriced
        plan_data = _plan_to_dict(plan, prices, type_name, conn=conn, input_basis=input_basis)
        # Override ME/TE in plan_data if entered manually
        if plan_data.get("blueprint"):
            plan_data["blueprint"]["me"] = int(me)
            plan_data["blueprint"]["te"] = te
        elif me_override is not None:
            plan_data["blueprint"] = {"kind": "—", "me": int(me), "te": te, "runs": "—", "manual": True}

        # Make-vs-buy decisions go to the UI in every mode (informational
        # tab). Only optimal mode acts on them: bought components are pruned
        # out of the manufacturing-steps tree — you don't run (or pay job
        # fees for) jobs whose output you buy off market.
        if plan.opt_decisions:
            plan_data["opt_decisions"] = [
                {
                    "type_id":    d.type_id,
                    "name":       d.name,
                    "quantity":   d.quantity,
                    "unit_price": (d.buy_cost / d.quantity)
                                  if (d.buy_cost is not None and d.quantity) else None,
                    "make_cost":  d.make_cost,
                    "buy_cost":   d.buy_cost,
                    "action":     d.action,
                    "savings":    d.savings,
                }
                for d in plan.opt_decisions
            ]
        if mode == "optimal" and plan.opt_decisions:
            buy_type_ids = {d.type_id for d in plan.opt_decisions if d.action == "buy"}
            if buy_type_ids:
                def _prune_bought(node):
                    kept = []
                    for c in node.children:
                        if c.type_id in buy_type_ids:
                            # bought → becomes a leaf (market purchase), no sub-jobs
                            c.children = []
                            c.is_leaf = True
                            c.activity = "raw"
                            kept.append(c)
                        else:
                            _prune_bought(c)
                            kept.append(c)
                    node.children = kept
                _prune_bought(root)

        plan_data["manufacturing_steps"] = _build_manufacturing_steps(root, prices, available, input_basis)
        # The materials above were costed as split jobs, so the job list has to
        # show the same split — otherwise the two views describe different builds.
        if _job_splits:
            _split_step_jobs(plan_data["manufacturing_steps"], _job_splits)

        # === Manufacturing fees === (fee parameters were resolved up-front,
        # before build_plan, so the make-vs-buy optimizer could weigh them.)

        total_job_fee = 0.0
        total_mfg_time_s = 0   # time of all manufacturing steps (sequential)
        total_rxn_time_s = 0

        # Bulk-fetch all blueprint data referenced by the manufacturing steps,
        # so each job doesn't hit DB 3× for its bp_id (materials, time, skills).
        all_bp_ids: set[int] = set()
        for step in plan_data["manufacturing_steps"]:
            for job in step["jobs"]:
                bp = job.get("blueprint_type_id")
                if bp:
                    all_bp_ids.add(bp)

        bp_materials_idx: dict[tuple[int, str], list] = {}
        bp_time_idx: dict[int, tuple[int, int]] = {}
        bp_skills_idx: dict[tuple[int, str], list] = {}
        if all_bp_ids:
            ph = ",".join("?" * len(all_bp_ids))
            ids_list = list(all_bp_ids)
            for mid, q, act, bp in conn.execute(
                f"SELECT material_type_id, quantity, activity, blueprint_type_id"
                f"  FROM sde_blueprint_materials WHERE blueprint_type_id IN ({ph})",
                ids_list,
            ).fetchall():
                bp_materials_idx.setdefault((bp, act), []).append((mid, q))
            for bp, mtime, rtime in conn.execute(
                f"SELECT blueprint_type_id, manufacturing_time, reaction_time"
                f"  FROM sde_blueprints WHERE blueprint_type_id IN ({ph})",
                ids_list,
            ).fetchall():
                bp_time_idx[bp] = (mtime, rtime)
            for bp, act, sk_id, req_lvl in conn.execute(
                f"SELECT blueprint_type_id, activity, skill_type_id, required_level"
                f"  FROM sde_blueprint_skills WHERE blueprint_type_id IN ({ph})"
                f"    AND skill_type_id NOT IN (3380, 3388)",
                ids_list,
            ).fetchall():
                bp_skills_idx.setdefault((bp, act), []).append((sk_id, req_lvl))

        # Memoize get_product_te_multiplier per (facility-id, type_id).
        # Same product appears across multiple steps when the resolver
        # aggregates duplicates — without the cache we re-classify it each
        # time and pay the rig_applies_to_product cost again.
        te_mult_cache: dict[tuple[int, int], float] = {}
        def _te_mult_for(prod_facility, type_id: int) -> float:
            key = (id(prod_facility), type_id)
            cached = te_mult_cache.get(key)
            if cached is not None:
                return cached
            val = get_product_te_multiplier(conn, prod_facility, type_id)
            te_mult_cache[key] = val
            return val

        # Slot capacity from the app-wide defaults. All zero (the default) means
        # unlimited, which reproduces the previous longest-job-per-level estimate.
        from app.manufacturing.schedule import SlotLimits as _SlotLimits
        _plan_defaults = app_defaults.get_defaults(conn)
        _slot_limits = _SlotLimits(
            manufacturing=int(_plan_defaults.get("manufacturing_slots") or 0),
            reaction=int(_plan_defaults.get("reaction_slots") or 0),
            capital=int(_plan_defaults.get("capital_slots") or 0),
        )
        _capital_groups = _capital_group_lookup(conn, plan_data["manufacturing_steps"])

        for step in plan_data["manufacturing_steps"]:
            step_mfg_time = 0
            step_rxn_time = 0
            # In "components" mode we buy the 1st level from the market — we pay
            # install fees only for the final job (assembling the product itself).
            skip_fee = (mode == "components" and not step.get("is_final"))
            for job in step["jobs"]:
                is_rxn   = job.get("activity") == "reaction"
                sci      = rxn_sci      if is_rxn else mfg_sci
                tax_rate = rxn_fac_tax_rate if is_rxn else fac_tax_rate
                cost_bonus = rxn_cost_bonus if is_rxn else mfg_cost_bonus

                # EIV must use the BASE quantities from the SDE (not ME-reduced)
                bp_id = job.get("blueprint_type_id")
                runs  = job.get("runs", 1) or 1
                if bp_id:
                    base_mats = bp_materials_idx.get(
                        (bp_id, job.get("activity", "manufacturing")), []
                    )
                    eiv = sum(adj_prices.get(m[0], 0.0) * m[1] * runs for m in base_mats)
                else:
                    eiv = sum(adj_prices.get(inp["type_id"], 0.0) * inp["quantity"]
                              for inp in job["inputs"])

                # Official formula: TIF = EIV × ((SCI × (1 - structure_cost_bonus)) + tax + SCC)
                job_fee = eiv * (sci * (1.0 - cost_bonus) + tax_rate + _SCC)
                job["eiv"] = eiv
                job["sci"] = sci
                job["tax_pct"] = round(tax_rate * 100, 2)
                job["job_fee"] = job_fee
                if not skip_fee:
                    total_job_fee += job_fee

                # Job duration
                if bp_id:
                    bp_time_row = bp_time_idx.get(bp_id)
                    base_time = (bp_time_row[1] if is_rxn else bp_time_row[0]) if bp_time_row else None
                    if base_time:
                        activity_name = job.get("activity", "manufacturing")
                        sci_mult, sci_details = _science_skill_mult(
                            conn, bp_id, activity_name, char_skills,
                            preloaded=bp_skills_idx.get((bp_id, activity_name)),
                        )
                        job_te = te if not is_rxn else 0
                        # Per-product TE multiplier — an Equipment TE rig doesn't speed up building a ship
                        prod_facility = rxn_facility if is_rxn else mfg_facility
                        prod_te_mult = _te_mult_for(prod_facility, job["type_id"])
                        job_secs = calc_job_time(
                            base_time=base_time,
                            runs=runs,
                            te=job_te,
                            industry_level=industry_level,
                            adv_industry_level=adv_industry_level,
                            facility_te_multiplier=prod_te_mult,
                            is_reaction=is_rxn,
                            science_skill_mult=sci_mult,
                            implant_time_pct=implant_mfg_pct,
                        )
                        job["facility_te_mult"] = prod_te_mult
                        job["job_duration_seconds"] = job_secs
                        job["job_duration"] = format_duration(job_secs)
                        job["science_skills"] = sci_details  # [(name, level, bonus_pct)]
                        if is_rxn:
                            step_rxn_time = max(step_rxn_time, job_secs)
                        else:
                            step_mfg_time = max(step_mfg_time, job_secs)

            # With slot limits configured, a level is not "as long as its
            # longest job" — that assumed unlimited parallel slots. Schedule the
            # level's jobs across the pools instead. With no limits set,
            # `schedule_level` returns the same longest-job figure as before.
            if _slot_limits.manufacturing or _slot_limits.reaction or _slot_limits.capital:
                step_mfg_time, step_rxn_time = _schedule_step(
                    step, _slot_limits, _capital_groups)

            total_mfg_time_s += step_mfg_time
            total_rxn_time_s += step_rxn_time

        # "Jobs to Run" — the same jobs bucketed by what they are, so a build
        # that expands into hundreds of installs stays readable.
        from app.manufacturing.schedule import group_jobs as _group_jobs
        plan_data["job_groups"] = _group_jobs(
            [job for step in plan_data["manufacturing_steps"] for job in step["jobs"]],
            _plan_group_ids(conn, plan_data["manufacturing_steps"]),
            end_product_id=type_id,
        )
        plan_data["total_job_count"] = sum(g["job_count"] for g in plan_data["job_groups"])

        # Collect unique science skills across all jobs for display in the header.
        # For the same skill across jobs we take the max required_level.
        _seen: dict[str, tuple[int, float, int]] = {}
        for step in plan_data.get("manufacturing_steps", []):
            for job in step.get("jobs", []):
                for sname, slevel, spct, sreq in job.get("science_skills", []):
                    prev = _seen.get(sname)
                    if prev is None:
                        _seen[sname] = (slevel, spct, sreq)
                    else:
                        _seen[sname] = (slevel, spct, max(prev[2], sreq))
        plan_data["all_science_skills"] = [
            (n, l, p, r) for n, (l, p, r) in sorted(_seen.items())
        ]

        # Required Industry / Adv Industry levels — max across all BPs in the plan
        bp_ids_in_plan: set[int] = set()
        for step in plan_data.get("manufacturing_steps", []):
            for job in step.get("jobs", []):
                bp_id_j = job.get("blueprint_type_id")
                if bp_id_j:
                    bp_ids_in_plan.add(int(bp_id_j))
        industry_required = 0
        adv_industry_required = 0
        if bp_ids_in_plan:
            ph = ",".join("?" * len(bp_ids_in_plan))
            req_rows = conn.execute(
                f"SELECT skill_type_id, MAX(required_level) FROM sde_blueprint_skills"
                f" WHERE blueprint_type_id IN ({ph}) AND skill_type_id IN (3380, 3388)"
                f" GROUP BY skill_type_id",
                tuple(bp_ids_in_plan),
            ).fetchall()
            for sid, lvl in req_rows:
                if sid == 3380:
                    industry_required = int(lvl)
                elif sid == 3388:
                    adv_industry_required = int(lvl)
        plan_data["industry_required"] = industry_required
        plan_data["adv_industry_required"] = adv_industry_required

        # Market price of all materials (regardless of stock)
        full_mat_cost = sum(
            m.get("total_price") or 0.0 for m in plan_data.get("materials", [])
        )
        # Price of only the missing materials (what needs to be bought)
        buy_cost = plan_data.get("total_buy") or 0.0
        rev = plan_data.get("revenue")

        # Selling is not free: sales tax always, broker's fee when listing an
        # order. Between 4.4% and 10.5% of revenue, which on a thin margin is
        # the whole margin — so it comes off both profit figures, not just the
        # headline one.
        _sell_costs = selling_costs(_plan_defaults)
        selling_cost = _sell_costs.on(rev) if rev is not None else 0.0

        # Market profit: revenue − all materials at market price − job fee − selling
        profit_market = (rev - full_mat_cost - total_job_fee - selling_cost) if rev is not None else None
        # Profit with stock: revenue − only missing materials − job fee − selling
        profit_stock  = (rev - buy_cost - total_job_fee - selling_cost) if rev is not None else None

        total_time_s = total_mfg_time_s + total_rxn_time_s
        plan_data["fees"] = {
            "solar_system_id":     solar_system_id,
            "rxn_solar_system_id": rxn_solar_system_id,
            "sep_rxn_station":     sep_rxn_station,
            "mfg_sci":             mfg_sci,
            "rxn_sci":             rxn_sci,
            "facility_tax":        fac_tax_pct,
            "rxn_facility_tax":    rxn_fac_tax_pct,
            "mfg_cost_bonus_pct":  round(mfg_cost_bonus * 100, 1),
            "rxn_cost_bonus_pct":  round(rxn_cost_bonus * 100, 1) if sep_rxn_station else None,
            "total_job_fee":       total_job_fee,
            "total_time_s":        total_time_s,
            "total_time":          format_duration(total_time_s) if total_time_s else None,
            "implant_mfg_pct":     implant_mfg_pct,
            "implant_mfg_name":    _implant_name_for_pct(implant_mfg_pct),
            "mfg_te_pct":          round((1 - mfg_te_mult) * 100, 1),
            "rxn_te_pct":          round((1 - rxn_te_mult) * 100, 1) if sep_rxn_station else None,
            "mfg_me_pct":          round((1 - mfg_me_mult) * 100, 2),
            "rxn_me_pct":          round((1 - rxn_me_mult) * 100, 2) if sep_rxn_station else None,
            "full_mat_cost":       full_mat_cost,
            "selling_cost":        selling_cost,
            "selling_cost_pct":    _sell_costs.pct,
            "sales_tax_pct":       _sell_costs.sales_tax * 100,
            "broker_fee_pct":      _sell_costs.broker_fee * 100,
            "sales_method":        _sell_costs.method,
            "profit_market":       profit_market,
            "profit_stock":        profit_stock,
        }

    except Exception as e:
        error = esi_error_message(e) or str(e)

    # Stock-source options (names via ESI, not bare IDs). Default = manufacturing
    # station unless the user selected explicitly.
    _stock_token = _get_valid_token_for(conn, plan_char_id_int) if plan_char_id_int else None
    stock_station_options = await _build_stock_station_options(
        conn, plan_char_id_int, _stock_token,
        selected_ids=stock_station_ids, default_station=station, explicit=stock_explicit,
    )
    location_ids = [o["location_id"] for o in stock_station_options]

    # Load the station name for display in the form
    loc_names = load_location_names_from_db(conn)
    station_name = loc_names.get(station, str(station))
    rxn_station_name = loc_names.get(reaction_station, str(reaction_station)) if reaction_station else ""

    # Best sell price of the product at the selling station (from station_volume_cache)
    sell_loc = selling_station if selling_station else station
    station_sell_price: float | None = None
    if plan_data and plan_data.get("product_type_id"):
        svols = get_cached_station_volumes(conn, sell_loc)
        if svols:
            entry = svols.get(plan_data["product_type_id"])
            if entry and entry[1]:
                station_sell_price = entry[1]
    selling_station_name = loc_names.get(sell_loc, str(sell_loc)) if sell_loc else ""

    conn.close()

    return _tr("plan.html", request, {
        "locations": location_ids,
        "stock_station_options": stock_station_options,
        "form_stock_stations": stock_stations,
        "result": plan_data,
        "error": error,
        "form_product": product,
        "form_station": station,
        "form_station_name": station_name,
        "form_rxn_station": reaction_station or "",
        "form_rxn_station_name": rxn_station_name,
        "form_qty": qty,
        "form_mode": mode,
        "form_input_basis": input_basis,
        "form_runs_per_job": runs_per_job,
        # After computing, always show the ROOT BP ME/TE (the actual values used in the plan) —
        # the user sees a concrete number instead of a placeholder.
        "form_me": str(int(me)),
        "form_te": str(int(te)),
        "form_facility_tax": facility_tax,
        "form_rxn_facility_tax": reaction_facility_tax if reaction_facility_tax.strip() else facility_tax,
        "form_facility_me_bonus": facility_me_bonus,
        "form_rxn_me_bonus": reaction_me_bonus,
        "station_sell_price": station_sell_price,
        "station_name": station_name,
        "selling_station_name": selling_station_name,
        "form_selling_station": selling_station or "",
        "form_selling_station_name": selling_station_name if selling_station else "",
        "form_industry":     form_industry,
        "form_adv_industry": form_adv_industry,
        "form_implant_mfg":  implant_mfg,
        "mfg_implant_options": _MFG_IMPLANT_OPTIONS,
        "plan_char_id":      plan_char_id_int,
    })


# location_flag values that mean "inside a ship" (fitted modules, cargo,
# drone/fighter bay, specialized bay). Such items are NOT counted as manufacturing
# stock — it makes no sense to strip individual ships' fit/cargo. Hangar,
# AutoFit/Unlocked/Locked (contents of hangar containers), on the other hand, are.
_SHIP_INTERNAL_FLAGS: frozenset[str] = frozenset({
    "Cargo", "DroneBay", "FleetHangar", "ShipHangar", "FighterBay",
    "FighterTube0", "FighterTube1", "FighterTube2", "FighterTube3", "FighterTube4",
    "HiddenModifiers",
    *(f"HiSlot{i}" for i in range(8)),
    *(f"MedSlot{i}" for i in range(8)),
    *(f"LoSlot{i}" for i in range(8)),
    *(f"RigSlot{i}" for i in range(8)),
    *(f"SubSystemSlot{i}" for i in range(8)),
})


def _is_ship_internal_flag(flag: str) -> bool:
    return flag in _SHIP_INTERNAL_FLAGS or flag.startswith("Specialized")


def _rollup_stock(rows: list[tuple]) -> dict[int, dict[int, int]]:
    """rows: (item_id, location_id, location_flag, type_id, quantity, is_singleton).

    Return {station_id: {type_id: total_qty}} — items rolled up to a real
    station/structure (container contents are summed onto their station),
    EXCLUDING singletons (ships, unique items) and everything inside ships
    (ship cargo / fittings / bays). station_id = the first ancestor in the chain
    that is no longer an owned item (= a real station or structure).
    """
    by_id = {r[0]: r for r in rows}
    result: dict[int, dict[int, int]] = {}
    for r in rows:
        item_id, loc_id, flag, type_id, qty, singleton = r
        if singleton:
            continue
        # Walk up the chain; if you hit a ship-internal flag anywhere,
        # it's ship contents → skip.
        node = r
        seen: set[int] = set()
        station = loc_id
        excluded = False
        for _ in range(32):
            if _is_ship_internal_flag(node[2]):
                excluded = True
                break
            parent_id = node[1]
            parent = by_id.get(parent_id)
            if parent is None:
                station = parent_id   # real station/structure
                break
            if parent_id in seen:
                station = parent_id
                break
            seen.add(parent_id)
            node = parent
        if excluded:
            continue
        d = result.setdefault(station, {})
        d[type_id] = d.get(type_id, 0) + qty
    return result


def _rollup_stock_from_charassets(assets) -> dict[int, dict[int, int]]:
    rows = [(a.item_id, a.location_id, a.location_flag, a.type_id, a.quantity, a.is_singleton)
            for a in assets]
    return _rollup_stock(rows)


def _rollup_stock_from_cache(raw: list[dict]) -> dict[int, dict[int, int]]:
    rows = [(a["item_id"], a["location_id"], a.get("location_flag", ""),
             a["type_id"], a["quantity"], a.get("is_singleton", False))
            for a in raw]
    return _rollup_stock(rows)


async def _build_stock_station_options(
    conn: sqlite3.Connection,
    plan_char_id: int | None,
    token: str | None,
    *,
    selected_ids: set[int],
    default_station: int,
    explicit: bool,
) -> list[dict]:
    """Stations where the planning character has non-singleton items — options for
    the stock-source picker. Names are resolved via ESI (resolve_station_names_bulk)
    so bare IDs aren't shown. `selected` = the user's explicit choice,
    otherwise default to the manufacturing station.
    """
    if not plan_char_id:
        return []
    raw = _load_assets_from_cache(conn, plan_char_id)
    # Roll up container contents onto their station and skip ship cargo/fittings.
    station_types = _rollup_stock_from_cache(raw)
    if not station_types:
        return []
    seen_types = {sid: set(types.keys()) for sid, types in station_types.items()}
    loc_ids = list(seen_types.keys())

    def _is_real(n: str | None, lid: int) -> bool:
        return bool(n) and not n.startswith("[") and n != str(lid)

    # The DB cache holds real names accumulated earlier (Assets resolves them
    # per-owner with a token and stores them here) — placeholders are never
    # stored in the DB. We use it as the primary source WITHOUT an ESI call.
    db_names = load_location_names_from_db(conn)

    # Resolve via ESI only for stations that don't have a real name yet — and only
    # with the planning character's token. Resolving all ~79 structures with the
    # tokens of ALL characters (as v0.6.1/0.6.2 did) generates a flood of 403
    # responses and ESI error-limits us (HTTP 420), which then also breaks product
    # resolution. resolve_station_name also remembers 403s and 420s so they don't repeat.
    resolved: dict[int, str] = {}
    unresolved = [lid for lid in loc_ids if not _is_real(db_names.get(lid), lid)]
    if unresolved:
        try:
            r = await resolve_station_names_bulk(unresolved, token=token, conn=conn)
            resolved = {lid: n for lid, n in r.items() if _is_real(n, lid)}
        except Exception:
            pass

    def _best_name(lid: int) -> str:
        if _is_real(db_names.get(lid), lid):
            return db_names[lid]
        if _is_real(resolved.get(lid), lid):
            return resolved[lid]
        return f"Private structure · {lid}"

    options = [
        {
            "location_id": lid,
            "name": _best_name(lid),
            "count": len(types),
            "selected": (lid in selected_ids) if explicit else (lid == default_station),
        }
        for lid, types in seen_types.items()
    ]
    options.sort(key=lambda o: (-o["count"], o["name"]))
    return options


def _derive_job_splits(
    conn: sqlite3.Connection,
    root,
    *,
    max_days: float,
    te: int,
    industry_level: int,
    adv_industry_level: int,
    mfg_facility,
    rxn_facility,
    char_skills: dict,
) -> dict[int, int]:
    """Runs-per-job per product, so no single job runs longer than `max_days`.

    A day limit produces a different run count for every product — a 2-hour
    reaction and a 3-day capital part hit the same ceiling at wildly different
    run counts — which is why this returns a map rather than one number.

    The per-run time is computed exactly as the job-duration loop below computes
    it (same TE rule, same skills, same per-product facility multiplier). It has
    to be: the split decides the material totals, so if this disagreed with the
    displayed job durations the Materials tab and the Jobs list would drift
    apart, which is the whole failure this two-pass exists to prevent.
    """
    from app.manufacturing.schedule import max_runs_per_job

    if not max_days or max_days <= 0:
        return {}

    seen: dict[int, tuple[int, str]] = {}       # type_id → (blueprint_id, activity)

    def walk(node):
        if node.is_leaf or not node.blueprint_type_id:
            return
        seen.setdefault(node.type_id,
                        (node.blueprint_type_id, node.activity or "manufacturing"))
        for child in node.children:
            walk(child)

    walk(root)
    if not seen:
        return {}

    bp_ids = {bp for bp, _ in seen.values()}
    ph = ",".join("?" * len(bp_ids))
    times = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT blueprint_type_id, manufacturing_time, reaction_time FROM sde_blueprints "
        f"WHERE blueprint_type_id IN ({ph})", list(bp_ids))}

    splits: dict[int, int] = {}
    for type_id, (bp_id, activity) in seen.items():
        row = times.get(bp_id)
        if not row:
            continue
        is_rxn = activity == "reaction"
        base_time = row[1] if is_rxn else row[0]
        if not base_time:
            continue
        sci_mult, _details = _science_skill_mult(conn, bp_id, activity, char_skills)
        per_run = calc_job_time(
            base_time=base_time,
            runs=1,
            te=0 if is_rxn else te,
            industry_level=industry_level,
            adv_industry_level=adv_industry_level,
            facility_te_multiplier=get_product_te_multiplier(
                conn, rxn_facility if is_rxn else mfg_facility, type_id),
            is_reaction=is_rxn,
            science_skill_mult=sci_mult,
        )
        limit = max_runs_per_job(per_run, max_days)
        if limit:
            splits[type_id] = limit
    return splits


def _split_step_jobs(steps: list[dict], splits: dict[int, int]) -> None:
    """Expands each aggregated job into the jobs you would actually install.

    `_build_manufacturing_steps` produces one entry per product carrying the
    whole run count. Once a day limit applies, that single 260-day row is a
    fiction — the materials were costed as ten separate jobs, so the job list
    has to show ten. Mutates `steps` in place, preserving order.

    Per-job input quantities are scaled by run share. They are indicative: ME
    rounds per job, so the true per-job draw varies by a unit here and there.
    The Materials tab is the authoritative total, and it is computed from the
    same splits.
    """
    for step in steps:
        expanded: list[dict] = []
        for job in step["jobs"]:
            per_job = splits.get(job["type_id"])
            total_runs = job.get("runs") or 0
            pieces = _schedule_split_runs(total_runs, per_job)
            if len(pieces) <= 1:
                expanded.append(job)
                continue
            for runs in pieces:
                share = runs / total_runs
                clone = dict(job)
                clone["runs"] = runs
                clone["quantity"] = round(job.get("quantity", 0) * share)
                if job.get("total_price"):
                    clone["total_price"] = job["total_price"] * share
                if job.get("input_cost"):
                    clone["input_cost"] = job["input_cost"] * share
                clone["inputs"] = [
                    {**inp,
                     "quantity": round(inp.get("quantity", 0) * share),
                     "total_price": (inp["total_price"] * share)
                                    if inp.get("total_price") else inp.get("total_price")}
                    for inp in job.get("inputs", [])
                ]
                clone["split_of"] = len(pieces)
                expanded.append(clone)
        step["jobs"] = expanded


def _schedule_split_runs(total_runs: int, per_job: int | None) -> list[int]:
    from app.manufacturing.schedule import split_runs
    return split_runs(total_runs, per_job)


def _plan_group_ids(conn: sqlite3.Connection, steps: list[dict]) -> dict[int, int]:
    """type_id → SDE group_id for everything in the plan.

    Classification is by group, never by product name — a rename in a patch
    must not silently reclassify a job.
    """
    ids = {job["type_id"] for step in steps for job in step["jobs"]}
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT type_id, group_id FROM sde_types WHERE type_id IN ({ph})", list(ids))}


def _capital_group_lookup(conn: sqlite3.Connection, steps: list[dict]) -> set[int]:
    """type_ids in the plan that are capital components."""
    from app.manufacturing.schedule import CAPITAL_COMPONENT_GROUPS

    return {tid for tid, gid in _plan_group_ids(conn, steps).items()
            if gid in CAPITAL_COMPONENT_GROUPS}


def _schedule_step(step: dict, limits, capital_ids: set[int]) -> tuple[int, int]:
    """(manufacturing seconds, reaction seconds) for one level, across slots.

    The two pools run concurrently, so the caller adds each to its own running
    total exactly as it did when the figure was "longest job in the level".
    """
    from app.manufacturing.schedule import Job, _pack, _pack_manufacturing

    reactions, manufacturing = [], []
    for job in step["jobs"]:
        secs = job.get("job_duration_seconds")
        if not secs:
            continue
        entry = Job(
            type_id=job["type_id"], name=job.get("name", ""),
            activity=job.get("activity", "manufacturing"),
            runs=job.get("runs", 1) or 1, seconds=secs,
            is_capital=job["type_id"] in capital_ids,
        )
        (reactions if entry.activity == "reaction" else manufacturing).append(entry)
    return _pack_manufacturing(manufacturing, limits), _pack(reactions, limits.reaction)


def _build_manufacturing_steps(root, prices: dict, available: dict,
                               input_basis: str = "sell") -> list[dict]:
    """
    Manufacturing steps: level 1 = manufactured first (everything from RAW), level N = last.
    Deduplicates the same type_id across branches, aggregates quantities.
    """
    from collections import defaultdict

    price_idx = 1 if input_basis == "buy" else 0
    level_memo: dict[int, int] = {}

    def manufacture_level(node) -> int:
        if node.is_leaf:
            return 0
        if node.type_id in level_memo:
            return level_memo[node.type_id]
        child_levels = [manufacture_level(c) for c in node.children]
        non_zero = [l for l in child_levels if l > 0]
        result = 1 + max(non_zero) if non_zero else 1
        level_memo[node.type_id] = result
        return result

    aggregated: dict[int, dict] = {}
    inputs_agg: dict[int, dict[int, dict]] = {}

    def collect(node):
        if node.is_leaf:
            return
        for child in node.children:
            collect(child)

        tid   = node.type_id
        level = manufacture_level(node)
        sell_p = prices.get(tid, (None, None))[price_idx]

        if tid not in aggregated:
            aggregated[tid] = {
                "type_id":           tid,
                "name":              node.name,
                "quantity":          node.quantity,
                "runs":              node.runs,
                "per_run":           getattr(node, "product_qty_per_run", 1),
                "blueprint_type_id": node.blueprint_type_id,
                "level":             level,
                "activity":          node.activity,
                "me":                node.me,
                "unit_price":        sell_p,
                "total_price":       sell_p * node.quantity if sell_p else None,
                "available":         available.get(tid, 0),
            }
            inputs_agg[tid] = {}
        else:
            aggregated[tid]["quantity"] += node.quantity
            # Recompute runs from aggregated quantity instead of summing per-branch
            # runs. Per-branch ceil() rounds up locally; summed it over-states the
            # total. Example: Helium Fuel Block (40/run) needed 5 in Carbon Polymers
            # branch + 5 in Dysporite branch — each rounded to 1 run → sum 2 runs
            # shown to user, but in reality 10 / 40 = 1 run suffices.
            from math import ceil
            per_run = aggregated[tid].get("per_run") or 1
            aggregated[tid]["runs"] = ceil(aggregated[tid]["quantity"] / per_run)
            if sell_p:
                aggregated[tid]["total_price"] = sell_p * aggregated[tid]["quantity"]

        for c in node.children:
            c_sell = prices.get(c.type_id, (None, None))[price_idx]
            if c.type_id not in inputs_agg[tid]:
                inputs_agg[tid][c.type_id] = {
                    "type_id":    c.type_id,
                    "name":       c.name,
                    "quantity":   c.quantity,
                    "is_leaf":    c.is_leaf,
                    "activity":   c.activity,
                    "unit_price": c_sell,
                    "total_price": c_sell * c.quantity if c_sell else None,
                    "available":  available.get(c.type_id, 0),
                }
            else:
                inputs_agg[tid][c.type_id]["quantity"] += c.quantity
                if c_sell:
                    inputs_agg[tid][c.type_id]["total_price"] = (
                        c_sell * inputs_agg[tid][c.type_id]["quantity"]
                    )

    collect(root)

    for tid, job in aggregated.items():
        job["inputs"] = sorted(inputs_agg[tid].values(), key=lambda x: x["name"])
        job["input_cost"] = sum(i["total_price"] for i in job["inputs"] if i["total_price"]) or None

    by_level: defaultdict[int, list] = defaultdict(list)
    for job in aggregated.values():
        by_level[job["level"]].append(job)

    max_level = max(by_level.keys()) if by_level else 1
    steps = []
    for level in sorted(by_level.keys()):
        jobs = sorted(by_level[level], key=lambda x: x["name"])
        steps.append({
            "step":       level,
            "jobs":       jobs,
            "total_cost": sum(j["total_price"] for j in jobs if j["total_price"]) or None,
            "is_final":   level == max_level,
        })
    return steps


def _plan_to_dict(plan, prices, type_name: str, conn: sqlite3.Connection | None = None,
                  input_basis: str = "sell") -> dict:
    price_idx = 1 if input_basis == "buy" else 0
    bp = plan.blueprint
    bp_info = None
    if bp:
        bp_info = {
            "kind": "BPO" if bp.is_original else "BPC",
            "me": plan.me,
            "te": plan.te,
            "runs": "∞" if bp.runs == -1 else bp.runs,
        }

    # Bulk-fetch group names for the "Type" column so the materials table
    # can be sorted by category.
    group_names: dict[int, str] = {}
    if conn is not None and plan.materials:
        ids = list({m.type_id for m in plan.materials})
        if ids:
            ph = ",".join("?" * len(ids))
            rows = conn.execute(
                f"""SELECT t.type_id, g.name
                    FROM sde_types t LEFT JOIN sde_groups g ON g.group_id = t.group_id
                    WHERE t.type_id IN ({ph})""",
                ids,
            ).fetchall()
            group_names = {r[0]: (r[1] or "—") for r in rows}

    materials = []
    for m in sorted(plan.materials, key=lambda x: (x.ok, x.coverage_pct)):
        in_p = prices.get(m.type_id, (None, None))[price_idx]
        materials.append({
            "type_id": m.type_id,
            "name": m.name,
            "group_name": group_names.get(m.type_id, "—"),
            "required": m.required,
            "available": m.available,
            "missing": m.missing,
            "ok": m.ok,
            "coverage_pct": m.coverage_pct,
            "unit_price": in_p,
            "total_price": in_p * m.required if in_p else None,
            "buy_price": in_p * m.missing if (in_p and m.missing > 0) else None,
        })

    total_buy = sum(m["buy_price"] for m in materials if m["buy_price"])
    # Revenue always uses the product's sell price (what you receive when
    # selling) — the input_basis toggle governs only what you PAY for inputs.
    sell_p, _ = prices.get(plan.product_type_id, (None, None))
    revenue = sell_p * plan.quantity if sell_p else None
    profit = (revenue - total_buy) if (revenue and total_buy) else None

    return {
        "product_name": type_name,
        "product_type_id": plan.product_type_id,
        "quantity": plan.quantity,
        "mode": plan.mode,
        "blueprint": bp_info,
        "location_id": plan.location_id,
        "can_manufacture": plan.can_manufacture,
        "total_missing_types": plan.total_missing_types,
        "materials": materials,
        "opt_total_cost": plan.opt_total_cost,
        "opt_naive_cost": plan.opt_naive_cost,
        "total_buy": total_buy,
        "sell_price": sell_p,
        "revenue": revenue,
        "profit": profit,
        # Expected cost of the invented blueprints in this tree. Deliberately NOT
        # folded into total_buy or profit: total_buy is the shopping list (what
        # you still have to acquire) and `profit` above already excludes job fees
        # for the same reason. Folding invention in alone would make that line
        # inconsistent in a new way instead of fixing it — see the note in §8 of
        # the design doc.
        "invention_cost": plan.invention_cost,
        "invention_unpriced": plan.invention_unpriced,
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
from app.web.routers import characters as characters_router  # noqa: E402
from app.web.routers import contracts as contracts_router  # noqa: E402
from app.web.routers import industry as industry_router  # noqa: E402
from app.web.routers import locations as locations_router  # noqa: E402
from app.web.routers import media as media_router  # noqa: E402
from app.web.routers import planets as planets_router  # noqa: E402
from app.web.routers import projects as projects_router  # noqa: E402

app.include_router(assets_router.router)
app.include_router(characters_router.router)
app.include_router(contracts_router.router)
app.include_router(industry_router.router)
app.include_router(locations_router.router)
app.include_router(media_router.router)
app.include_router(planets_router.router)
app.include_router(prices_router.router)
app.include_router(projects_router.router)
