"""Shared request-scoped plumbing: the database connection, the template
environment and its filters, and the active character.

This module exists for W6 (`docs/design-hosted-v2.md` §11), which splits
`app/web/main.py` into routers. Every router needs `get_conn()` and `_tr()`,
and both lived in `main.py` — so a router importing them would import the
module that imports the router. This is the leaf that breaks that cycle.

Nothing here may import `app.web.main`, now or later. That is the whole
property the module is for.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sqlite3
import time as _time
from pathlib import Path

import httpx

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.esi.client import esi_client
from app.auth.token_store import (
    list_characters,
    get_character_row,
    get_valid_token as _get_valid_token_for,
)
from app.db.schema import (
    ensure_schema as ensure_db_schema,
    ensure_sde_schema,
)

# Path resolution. EVE_APP_DIR is the writable data directory and is what a
# deployment sets; EVE_BUNDLE_DIR is a leftover seam from the retired desktop
# build, where read-only bundled files lived apart from writable ones. Both
# default to the project root, which is correct for a server install.
_APP_DIR = os.environ.get("EVE_APP_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_BUNDLE_DIR = os.environ.get("EVE_BUNDLE_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

# Frozen at import. Every *other* place that needs this path resolves it at
# call time (app/auth/esi_oauth.py, app/web/bootstrap.py), and the difference
# matters: a module imported before EVE_APP_DIR is set binds whatever was
# there, permanently. That is how the test suite spent a day writing to a
# real database. tests/conftest.py::pytest_collection_finish is the net.
DB_ABS = os.path.join(_APP_DIR, "eve_cache.db")
TEMPLATES_DIR = Path(_BUNDLE_DIR) / "app" / "web" / "templates"
STATIC_DIR = Path(_BUNDLE_DIR) / "app" / "web" / "static"

# Set to True once SDE tables are confirmed present. Guards the setup gate.
_SDE_READY: list[bool] = [False]

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _deny(request: Request, status: int, message: str):
    """Refuse a request the way its caller can understand."""
    from fastapi.responses import JSONResponse, PlainTextResponse

    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": message}, status_code=status)
    if status == 401 and _wants_html(request):
        return RedirectResponse("/auth/login", status_code=303)
    return PlainTextResponse(message, status_code=status)


def _isk(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:,.2f}".replace(",", " ")


def _isk0(v: float | None) -> str:
    """Whole ISK, no decimals, space thousands (used where cents are just noise)."""
    if v is None:
        return "N/A"
    try:
        return f"{round(float(v)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _format_number(v) -> str:
    try:
        return f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _format_date(v) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(v)).strftime('%d.%m.%Y %H:%M')
    except Exception:
        return str(v)


def _ts_ago(ts: float) -> str:
    """Human-readable relative time from Unix timestamp."""
    try:
        delta = int(_time.time() - float(ts))
    except (TypeError, ValueError):
        return "?"
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def _ts_to_str(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ""


def _price_eu(v) -> str:
    """Default price format: <10k keeps 2 decimals, >=10k drops them. Space thousands."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    s = f"{v:,.2f}" if abs(v) < 10000 else f"{v:,.0f}"
    return s.replace(",", " ")


def _count_eu(v) -> str:
    """Integer count (volume / available): no decimals, space thousands."""
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return "—"
    return f"{v:,}".replace(",", " ")


def _age_short(ts) -> str:
    """Compact relative age of a timestamp: 'now' / '5m' / '10h' / '2d'.
    Returns '—' when there's no timestamp (never fetched)."""
    try:
        delta = int(_time.time() - float(ts))
    except (TypeError, ValueError):
        return "—"
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


templates.env.filters["isk"] = _isk
templates.env.filters["isk0"] = _isk0
templates.env.filters["format_number"] = _format_number
templates.env.filters["format_date"] = _format_date
templates.env.filters["age_short"] = _age_short
templates.env.filters["price_eu"] = _price_eu
templates.env.filters["count_eu"] = _count_eu
templates.env.filters["ts_ago"] = _ts_ago
templates.env.filters["ts_to_str"] = _ts_to_str


# `get_conn()` is on the hot path — runs on every request, so the table
# bootstrap must not. `ensure_db_schema()` memoizes per database file: the
# first connection in the process builds the schema, the rest just open the
# DB. It replaces a scattered set of ensure_*() calls that between them built
# only the twelve tables the startup path happened to know about — the rest
# appeared later, on whichever page first needed them.
def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bootstrap every table the app owns. Idempotent and cheap to re-call."""
    ensure_db_schema(conn)
    # Static data is created empty here so a query against a table CCP's
    # importer has not filled yet returns no rows instead of raising —
    # `import_sde.py` is what fills them.
    ensure_sde_schema(conn)


def get_conn() -> sqlite3.Connection:
    # WAL + a long busy timeout so concurrent work never trips "database is
    # locked". In the default rollback-journal mode a writer blocks all readers,
    # so the burst when a character is added (background sync writing large asset
    # caches) collided with rotating-refresh-token writes — a commit that waited
    # past the timeout raised, the token came back None, and the dashboard showed
    # no location / skill training for every character. WAL lets readers and one
    # writer run concurrently; the timeout absorbs brief writer-writer waits.
    conn = sqlite3.connect(DB_ABS, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    ensure_schema(conn)   # memoized per database file; a no-op after the first
    return conn


# ---------------------------------------------------------------------------
# Active character helpers (cookie-based)
# ---------------------------------------------------------------------------

ACTIVE_COOKIE = "active_char"


def get_active_character_id(request: Request, conn: sqlite3.Connection | None = None) -> int | None:
    """Return the active character id from cookie, or fall back to first char in DB."""
    cookie = request.cookies.get(ACTIVE_COOKIE) if request else None
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if cookie:
            try:
                cid = int(cookie)
            except ValueError:
                cid = None
            if cid and get_character_row(conn, cid):
                return cid
        chars = list_characters(conn)
        return chars[0][0] if chars else None
    finally:
        if own_conn:
            conn.close()


def get_active_character(request: Request, conn: sqlite3.Connection | None = None) -> tuple[int, str] | None:
    """Return (char_id, char_name) for the active character, or None."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        cid = get_active_character_id(request, conn)
        if cid is None:
            return None
        row = get_character_row(conn, cid)
        if row:
            return (row["character_id"], row["character_name"])
        return None
    finally:
        if own_conn:
            conn.close()


def get_active_token(request: Request, conn: sqlite3.Connection | None = None) -> str | None:
    """Return a fresh access token for the active character."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        cid = get_active_character_id(request, conn)
        if cid is None:
            return None
        return _get_valid_token_for(conn, cid)
    finally:
        if own_conn:
            conn.close()


def get_token_for(character_id: int, conn: sqlite3.Connection | None = None) -> str | None:
    """Return a fresh access token for a specific character."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        return _get_valid_token_for(conn, character_id)
    finally:
        if own_conn:
            conn.close()


def _tr(name: str, request: Request, context: dict) -> HTMLResponse:
    """Starlette's new API: request as the first argument."""
    conn = get_conn()
    try:
        active = get_active_character(request, conn)
        all_chars = list_characters(conn)
    finally:
        conn.close()
    context.setdefault("character", active)
    context.setdefault("all_characters", all_chars)
    context.setdefault("active_char_id", active[0] if active else None)
    # Every rendered page carries the session's CSRF token, so base.html can put
    # it in a <meta> for the fetch wrapper and forms can put it in a hidden field.
    session = getattr(request.state, "session", None)
    context.setdefault("csrf_token", session["csrf_token"] if session else "")
    return templates.TemplateResponse(request, name, context)


async def _ensure_groups_populated(conn: sqlite3.Connection) -> None:
    """Populate sde_groups via ESI /universe/groups/{id}/ with concurrency limit.

    Top-up semantics: fetches only groups referenced by sde_types that are
    MISSING from sde_groups. The previous all-or-nothing early return meant
    a new expansion's groups (e.g. 5120 Command Carrier) never got added for
    existing users — and rig_applies_to_product's INNER JOIN on sde_groups
    then silently disabled all rig bonuses for those products.
    """
    group_ids = [r[0] for r in conn.execute(
        """SELECT DISTINCT t.group_id FROM sde_types t
           LEFT JOIN sde_groups g ON g.group_id = t.group_id
           WHERE t.group_id > 0 AND t.published = 1 AND g.group_id IS NULL"""
    ).fetchall()]
    if not group_ids:
        return

    sem = asyncio.Semaphore(50)

    async def _fetch(client: httpx.AsyncClient, gid: int):
        async with sem:
            try:
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/groups/{gid}/",
                    params={"datasource": "tranquility"},
                    timeout=10,
                )
                if r.status_code == 200:
                    d = r.json()
                    if d.get("published", True):
                        return (gid, d["name"])
            except Exception:
                pass
            return None

    async with esi_client() as client:
        results = await asyncio.gather(*[_fetch(client, gid) for gid in group_ids])

    for row in results:
        if row:
            conn.execute("INSERT INTO sde_groups VALUES (?,?) ON CONFLICT (group_id) DO UPDATE SET name=excluded.name", row)
    conn.commit()


def _load_blueprints_from_cache(conn: sqlite3.Connection, char_id: int) -> list[dict]:
    row = conn.execute(
        "SELECT data_json FROM char_blueprints_cache WHERE character_id=?", (char_id,)
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0])


def _load_assets_from_cache(conn: sqlite3.Connection, char_id: int) -> list[dict]:
    """Load assets straight from the JSON cache without an ESI call."""
    row = conn.execute(
        "SELECT data_json FROM char_assets_cache WHERE character_id=?", (char_id,)
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0])


def _load_corp_assets_from_cache(conn: sqlite3.Connection, corp_id: int) -> list[dict]:
    row = conn.execute(
        "SELECT data_json FROM corp_assets_cache WHERE corporation_id=?", (corp_id,)
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0])


async def _resolve_party_names(ids: set[int]) -> dict[int, str]:
    """Resolve char/corp/alliance/station/type IDs to names via ESI
    /universe/names/. The endpoint can't handle player structures (>1e12) — we skip them.
    """
    ids = {i for i in ids if i and i < 1_000_000_000_000}
    out: dict[int, str] = {}
    if not ids:
        return out
    id_list = list(ids)
    async with esi_client(timeout=10) as client:
        for i in range(0, len(id_list), 1000):  # ESI limit 1000/req
            chunk = id_list[i:i + 1000]
            try:
                r = await client.post(
                    "https://esi.evetech.net/latest/universe/names/",
                    json=chunk, headers={"Accept": "application/json"},
                )
                if r.status_code == 200:
                    for item in r.json():
                        out[item["id"]] = item["name"]
            except Exception:
                pass
    return out


def _container_display_name(custom_name: str, type_name: str, container_id: int) -> str:
    """Label for a container or an assembled ship: "custom name (Type)".

    The bracket is a SIGNAL, not decoration: only assembled ships and in-use
    containers reach this function, because a row here exists solely for
    something holding other items. Repacked hulls are ordinary stack rows
    elsewhere showing the plain type name. So bracket = assembled, no bracket =
    repacked, readable at a glance.

    That is why the type is appended unconditionally — even for a ship named
    exactly "Hulk", which becomes "Hulk (Hulk)". Every attempt to suppress the
    "redundant" case has been wrong: a substring test hid the hull from every
    ship named Hulk1/Hulk2/…, and a whole-word test would do the same to
    "Hulk 1". No inspection of the name's content, no guessing.

    The one row without a bracket is an item nobody named, where the label is the
    bare type and there is nothing to disambiguate it from. ESI reports the
    literal string "None" for such an item, which is treated as no name.
    """
    custom = (custom_name or "").strip()
    if custom.lower() == "none":
        custom = ""
    type_name = (type_name or "").strip()
    if not type_name:
        return custom or f"Container {container_id}"
    if not custom:
        return type_name
    return f"{custom} ({type_name})"
