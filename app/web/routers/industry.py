"""Industry: running jobs, the reactions board, and the margin tracker.

Moved out of `main.py` unchanged (W6). The three pages sit together because
they answer the same question from different angles — what is in flight, what
is worth reacting, what is worth building — and because the reactions board and
the margin tracker share the cache-only discipline §11 wants everywhere: a full
board costs no ESI calls.
"""
from __future__ import annotations

import asyncio
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.token_store import list_characters
from app.character import jobs as jobs_api
from app.character.skills import get_cached_skills
from app.esi.client import esi_client
from app.web import margins_helper, reactions_helper
from app.db.location import database_path
from app.web.deps import _tr, _valid_token_async, get_conn
from app.web.location_resolver import (
    load_location_names_from_db,
    resolve_station_names_bulk,
)

router = APIRouter()


# ── Industry Jobs ─────────────────────────────────────────────────────────────

# Industry job slots: mapping of ESI activity_id → slot category and the skills
# that determine capacity (base 1 + level of both skills, max 11 per category).
_SLOT_CATEGORY = {
    1: "manufacturing",                       # Manufacturing
    3: "science", 4: "science",               # TE / ME research
    5: "science", 8: "science",               # Copying / Invention
    9: "reactions", 11: "reactions",          # Reactions
}
_SLOT_SKILLS = {
    "manufacturing": (3387, 24625),   # Mass Production, Advanced Mass Production
    "science":       (3406, 24624),   # Laboratory Operation, Advanced Laboratory Operation
    "reactions":     (45748, 45749),  # Mass Reactions, Advanced Mass Reactions
}
_SLOT_ORDER = (
    ("manufacturing", "Manufacturing"),
    ("science", "Science"),
    ("reactions", "Reactions"),
)


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    conn = get_conn()
    chars = list_characters(conn)
    if not chars:
        conn.close()
        return _tr("jobs.html", request, {"groups": [], "error": "You are not signed in.",
                                          "total_active": 0})

    # Fetch all characters' jobs concurrently. _one returns None (not []) when
    # the fetch could not run — no token or an ESI error — so a transient
    # failure (e.g. during a background Sync All) isn't shown as "no jobs".
    async def _one(cid: int):
        try:
            tok = await _valid_token_async(cid)
            if not tok:
                return cid, None
            async with esi_client() as client:
                return cid, await jobs_api.fetch_industry_jobs(client, cid, tok)
        except Exception:
            return cid, None

    raw_results = await asyncio.gather(*[_one(cid) for cid, _ in chars])
    fetch_failed = any(jl is None for _cid, jl in raw_results)
    results = [(cid, jl or []) for cid, jl in raw_results]
    char_name = {cid: name for cid, name in chars}

    # Collect type_ids (product/blueprint) and facility_id for resolution
    all_type_ids: set[int] = set()
    all_loc_ids: set[int] = set()
    for _cid, jl in results:
        for j in jl:
            if j.get("product_type_id"):
                all_type_ids.add(j["product_type_id"])
            if j.get("blueprint_type_id"):
                all_type_ids.add(j["blueprint_type_id"])
            if j.get("facility_id"):
                all_loc_ids.add(j["facility_id"])

    type_names: dict[int, str] = {}
    if all_type_ids:
        ph = ",".join("?" * len(all_type_ids))
        type_names = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(all_type_ids)
        ).fetchall()}
    loc_names: dict[int, str] = {}
    if all_loc_ids:
        # One refresh per character, concurrently, instead of up to two each
        # in series: the generator called the blocking version twice per
        # character — once for the condition and once for the value.
        tokens = await asyncio.gather(*[_valid_token_async(cid) for cid, _ in chars])
        any_tok = next((t for t in tokens if t), None)
        try:
            loc_names = await resolve_station_names_bulk(list(all_loc_ids), token=any_tok, conn=conn)
        except Exception:
            loc_names = load_location_names_from_db(conn)

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def _decorate_job(j: dict) -> dict:
        status = j.get("status", "")
        end = j.get("end_date", "")
        remaining = ""
        is_ready = False
        try:
            end_dt = _dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
            delta = end_dt - now
            if delta.total_seconds() <= 0:
                remaining = "Ready"
                is_ready = True
            else:
                secs = int(delta.total_seconds())
                d, rem = divmod(secs, 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                remaining = (f"{d}d " if d else "") + (f"{h}h " if (d or h) else "") + f"{m}m"
        except Exception:
            pass
        # Icon: the image server uses /bp for blueprints (not /icon — that returns
        # HTTP 400). We detect a blueprint by name (covers invention too, where
        # product_type_id is the produced BPC copy = also a blueprint).
        prod_id = j.get("product_type_id")
        bp_id = j.get("blueprint_type_id")
        icon_id = prod_id or bp_id
        prod = type_names.get(prod_id) or type_names.get(bp_id, f"#{bp_id}")
        is_bp = bool(prod) and prod.endswith("Blueprint")
        return {
            "activity": jobs_api.activity_label(j.get("activity_id", 0)),
            "product": prod,
            "icon_id": icon_id,
            "is_blueprint": is_bp,
            "runs": j.get("runs", 0),
            "location": loc_names.get(j.get("facility_id"), str(j.get("facility_id", ""))),
            "start_date": j.get("start_date", ""),
            "end_date": end,
            "remaining": remaining,
            "is_ready": is_ready,
            "status": status,
        }

    # Active = status active or ready (not yet delivered). We hide the rest.
    groups = []
    total_active = 0
    for cid, jl in results:
        # active/paused/ready = jobs that still hold a slot (ready = finished,
        # not delivered). delivered/cancelled/reverted don't block a slot.
        active = [j for j in jl if j.get("status") in ("active", "paused", "ready")]
        decorated = [_decorate_job(j) for j in active]
        # the ones finishing soonest first
        decorated.sort(key=lambda x: x["end_date"])
        total_active += len(decorated)

        # Slot occupancy by category (how many of how many). Max = base 1 +
        # both skill levels; None if the skills aren't synced yet.
        skills = get_cached_skills(conn, cid)
        used = {"manufacturing": 0, "science": 0, "reactions": 0}
        for j in active:
            cat = _SLOT_CATEGORY.get(j.get("activity_id", 0))
            if cat:
                used[cat] += 1
        slots = []
        for cat, label in _SLOT_ORDER:
            sa, sb = _SLOT_SKILLS[cat]
            mx = (1 + skills.get(sa, 0) + skills.get(sb, 0)) if skills else None
            slots.append({"label": label, "used": used[cat], "max": mx})

        groups.append({
            "char_id": cid,
            "char_name": char_name.get(cid, str(cid)),
            "jobs": decorated,
            "slots": slots,
        })
    # characters with the most jobs first
    groups.sort(key=lambda g: -len(g["jobs"]))

    conn.close()
    return _tr("jobs.html", request, {
        "groups": groups, "error": None, "total_active": total_active,
        "fetch_failed": fetch_failed,
    })


@router.get("/reactions", response_class=HTMLResponse)
async def reactions_page(request: Request, sort: str = "", dir: str = "",
                         group: str = ""):
    """Reactions board — the whole reaction space, priced and ranked.

    Cache-only like /margins, so a full board costs no ESI calls. Unlike the
    margin tracker there is no watchlist and no snapshot history: the space is
    119 products and pricing all of them measured well under a second, so the
    page just recomputes rather than storing what it last thought.
    """
    conn = get_conn()
    try:
        view = reactions_helper.build_board(
            conn, database_path(),
            sort=sort or reactions_helper.DEFAULT_SORT,
            direction=dir or reactions_helper.DEFAULT_DIR,
            group=group,
        )
    finally:
        conn.close()
    return _tr("reactions.html", request, view)


@router.get("/margins", response_class=HTMLResponse)
async def margins_page(request: Request, msg: str = ""):
    """Margin Tracker — a persistent watchlist of build margins.

    Prices entirely from cache (market, adjusted prices, cost indices), so
    rendering a watchlist of any size costs no ESI calls. Refresh the numbers
    by refreshing prices on /prices as usual.
    """
    conn = get_conn()
    try:
        view = margins_helper.build_view_model(conn, database_path(), message=msg or None)
    finally:
        conn.close()
    return _tr("margins.html", request, view)


@router.post("/margins/add")
async def margins_add(product: str = Form(...), me: str = Form("0"), te: str = Form("0")):
    conn = get_conn()
    try:
        row = _resolve_product_name(conn, product)
        if row is None:
            msg = f"No item named “{product}”."
        else:
            _ok, msg = margins_helper.add_item(
                conn, row[0], _safe_int(me, 0), _safe_int(te, 0))
    finally:
        conn.close()
    return RedirectResponse(f"/margins?msg={quote(msg)}", status_code=303)


@router.post("/margins/remove")
async def margins_remove(item_id: int = Form(...)):
    conn = get_conn()
    try:
        margins_helper.remove_item(conn, item_id)
    finally:
        conn.close()
    return RedirectResponse("/margins", status_code=303)


@router.post("/margins/clear")
async def margins_clear():
    conn = get_conn()
    try:
        margins_helper.clear_all(conn)
    finally:
        conn.close()
    return RedirectResponse("/margins", status_code=303)


def _safe_int(raw: str, fallback: int) -> int:
    """ME/TE parser. Negative values are legitimate — an unresearched BPC copy
    from a bad invention run really does cost more materials."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


def _resolve_product_name(conn: sqlite3.Connection, text: str):
    """Exact name, then a prefix match, then the raw type_id."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return conn.execute(
            "SELECT type_id, name FROM sde_types WHERE type_id=?", (int(text),)).fetchone()
    row = conn.execute(
        "SELECT type_id, name FROM sde_types WHERE LOWER(name)=?", (text.lower(),)).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT type_id, name FROM sde_types WHERE LOWER(name) LIKE ? "
        "ORDER BY LENGTH(name) LIMIT 1", (text.lower() + "%",)).fetchone()
