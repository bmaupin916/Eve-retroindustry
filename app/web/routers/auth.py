"""Sign-in, first-run setup, and settings.

Moved out of `main.py` unchanged (W6). One router because they are one flow:
a fresh install has no client id, so /setup/client-id feeds /auth/login, which
feeds /callback, which kicks off the first sync, which is what /auth/sync
watches. Settings is the same surface once you are in.

The post-login sync progress state lives here for the same reason. It is
module-level mutable on purpose — the loading screen polls /api/sync-status
while a background task writes it — so there is exactly one of it, and
`tests/test_sync_progress.py` reads this module's copy.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time as _time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.auth.esi_oauth import (
    LoginError,
    begin_login,
    callback_url,
    complete_login,
)
from app.auth.token_store import (
    delete_character,
    update_corporation_id,
    update_last_sync,
    get_character_row,
    has_any_character,
    list_characters,
)
from app.character.assets import fetch_assets, fetch_corp_assets
from app.character.blueprints import fetch_blueprints
from app.character.skills import fetch_skills
from app.esi.client import esi_client
from app.manufacturing import invention
from app.market.prices import TRADE_HUBS
from app.sync import worker as sync_worker
from app.web import app_defaults, security
from app.web.deps import ACTIVE_COOKIE, _tr, _valid_token_async, get_conn
from app.web.location_resolver import resolve_station_names_bulk

router = APIRouter()


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


# ---------------------------------------------------------------------------
# First-run setup routes
# ---------------------------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse)
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


@router.get("/setup/client-id", response_class=HTMLResponse)
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


@router.post("/setup/client-id")
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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/auth/login")
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


@router.get("/callback")
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
                sync_worker.wake()
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

    # The caches for a character nobody has synced are empty, and the pages
    # that read them say so rather than fetching. Ask for a round now instead
    # of letting the new character wait out the current interval.
    sync_worker.wake()

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

@router.get("/auth/sync", response_class=HTMLResponse)
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


@router.get("/auth/bootstrap")
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


@router.post("/auth/logout")
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


@router.get("/api/sync-status")
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

@router.post("/api/characters/{char_id}/activate")
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


@router.delete("/api/characters/{char_id}")
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


@router.post("/api/sync/start")
async def api_sync_start():
    """Manually trigger an ESI sync for all characters."""
    if _sync_state["running"]:
        return {"ok": False, "error": "Already running"}
    _sync_reset()
    asyncio.create_task(_bg_initial_sync())
    return {"ok": True}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.auth.token_store import get_client_id
    from app.auth.esi_oauth import callback_url, SCOPES
    conn = get_conn()
    try:
        # `app_defaults` is converted; this router is not. It gets its own
        # connection from the engine rather than the raw one — same database,
        # same committed state, and both styles coexist deliberately
        # (`test_both_connection_styles_work_on_one_database`).
        from app.db.conn import connect
        with connect() as _defaults_conn:
            defaults = app_defaults.get_defaults(_defaults_conn)
        station_options = _industry_station_options(conn)
        # Only the eight canonical decryptors: the faction-flavoured duplicates
        # and the ancient-relic ones behave identically or belong to reverse
        # engineering, and 64 entries would make the picker unusable.
        from app.db.conn import connect
        with connect() as _sde:
            _decs = invention.list_decryptors(_sde)
        decryptors = [d for d in _decs
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


@router.post("/api/settings/defaults")
async def api_save_defaults(request: Request):
    """Saves the app-wide industry defaults used by the margin tracker and
    pre-filled into the /plan form."""
    body = await request.json()
    # Nothing else in this handler touches the database, so it goes entirely
    # through the engine rather than opening a raw connection to hand over.
    from app.db.conn import connect
    with connect() as conn:
        saved = app_defaults.save_defaults(conn, body)
    return {"ok": True, "defaults": saved}


@router.post("/api/settings/client-id")
async def api_save_client_id(request: Request):
    body = await request.json()
    cid = body.get("client_id", "").strip()
    if not cid:
        return {"ok": False, "error": "Client ID cannot be empty."}
    from app.auth.token_store import save_client_id
    save_client_id(cid)
    return {"ok": True}
