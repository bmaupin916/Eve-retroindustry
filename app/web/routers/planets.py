"""Planetary industry: the colonies page, the PI planner, and the extractor
alerts behind the dashboard tile and the nav badge.

Moved out of `main.py` unchanged (W6). The whole PI cluster moves together —
the colony fetch, the planet-name resolver, the cache writer and the alert
summary are used by these four routes and nothing else.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time as _time

import httpx

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth.token_store import (
    list_characters,
    get_valid_token as _get_valid_token_for,
)
from app.character import planets as planets_api
from app.esi.client import esi_client
from app.web import pi_planner_helper
from app.db.location import database_path
from app.db.schema import ensure_schema as ensure_db_schema
from app.web.deps import _tr, get_conn

router = APIRouter()


async def _fetch_pi_colonies(conn: sqlite3.Connection, chars) -> list:
    """Colony list + per-planet detail for every character, concurrently.

    The single PI fetch path in the app: /planets and /pi-planner both call
    this, so the two ESI endpoints, the token handling and the "forbidden"
    contract live in exactly one place.

    Returns [(char_id, result)] where result is one of:
      (colonies, details) — `details` aligned positionally with `colonies`
      "forbidden"         — token predates the PI scope; prompt a re-auth
      None or []          — no token, no colonies, or the fetch failed
    """
    async def _one(cid: int):
        try:
            tok = _get_valid_token_for(conn, cid)
            if not tok:
                return cid, None
            async with esi_client() as client:
                colonies = await planets_api.fetch_planets(client, cid, tok)
                if colonies == "forbidden" or colonies is None or not colonies:
                    return cid, colonies
                details = await asyncio.gather(*[
                    planets_api.fetch_planet_detail(client, cid, c["planet_id"], tok)
                    for c in colonies], return_exceptions=True)
                return cid, (colonies, details)
        except Exception:
            return cid, None

    return await asyncio.gather(*[_one(cid) for cid, _ in chars])


@router.get("/planets", response_class=HTMLResponse)
async def planets_page(request: Request):
    """Planetary Interaction — colonies per character with extractor expiry
    countdowns (à la RIFT: the point is knowing when to go reset PI)."""
    conn = get_conn()
    chars = list_characters(conn)
    if not chars:
        conn.close()
        return _tr("planets.html", request, {
            "groups": [], "error": "You are not signed in.",
            "total_extractors": 0, "expiring_soon": 0, "needs_relogin": []})

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    results = await _fetch_pi_colonies(conn, chars)
    char_name = {cid: name for cid, name in chars}

    # Ids to resolve: planet names (per-planet endpoint — /universe/names can't do
    # planets), product type names (SDE).
    planet_ids: set[int] = set()
    type_ids: set[int] = set()
    schematic_ids: set[int] = set()
    for _cid, res in results:
        if not res or isinstance(res, str):
            continue
        colonies, details = res
        for c in colonies:
            planet_ids.add(c["planet_id"])
        for d in details:
            if isinstance(d, dict):
                for pin in d.get("pins", []):
                    ed = pin.get("extractor_details") or {}
                    if ed.get("product_type_id"):
                        type_ids.add(ed["product_type_id"])
                    for cont in (pin.get("contents") or []):
                        if cont.get("type_id"):
                            type_ids.add(cont["type_id"])
                    if pin.get("schematic_id"):
                        schematic_ids.add(pin["schematic_id"])

    # Factory schematics from the SDE (output + inputs + cycle) — powers the
    # production-chain view. Adds their type_ids so names resolve below.
    # The table arrives with v0.8.106; an older eve_cache.db that predates the
    # startup SDE-refresh won't have it yet, so degrade gracefully (colonies
    # still render, just without production chains) instead of 500-ing.
    schematics: dict[int, dict] = {}
    _has_schematics = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sde_planet_schematics'"
    ).fetchone() is not None
    if schematic_ids and _has_schematics:
        sph = ",".join("?" * len(schematic_ids))
        for sid, nm, cyc, out_tid, out_qty in conn.execute(
            f"SELECT schematic_id, name, cycle_time, output_type_id, output_qty "
            f"FROM sde_planet_schematics WHERE schematic_id IN ({sph})", list(schematic_ids)
        ).fetchall():
            schematics[sid] = {"name": nm, "cycle_time": cyc or 0,
                               "output_id": out_tid, "output_qty": out_qty or 0, "inputs": []}
            if out_tid:
                type_ids.add(out_tid)
        for sid, tid, qty in conn.execute(
            f"SELECT schematic_id, type_id, quantity FROM sde_planet_schematic_materials "
            f"WHERE schematic_id IN ({sph})", list(schematic_ids)
        ).fetchall():
            if sid in schematics:
                schematics[sid]["inputs"].append({"type_id": tid, "qty": qty})
                type_ids.add(tid)

    type_names: dict[int, str] = {}
    if type_ids:
        ph = ",".join("?" * len(type_ids))
        type_names = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(type_ids)
        ).fetchall()}

    # Sell prices (for the est. output-value/day hint) — the Jita cache.
    price_map: dict[int, float] = {}
    if type_ids:
        ph = ",".join("?" * len(type_ids))
        price_map = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, sell_price FROM market_price_cache "
            f"WHERE type_id IN ({ph}) AND sell_price IS NOT NULL", list(type_ids)
        ).fetchall()}

    # Planet names ("Jita IV", already includes the system). Resolved through the
    # shared cache: names never change, so only ids we've never seen cost an ESI
    # call — this page used to re-fetch every planet on every visit.
    planet_names = await _resolve_planet_names(conn, planet_ids)

    def _rem(iso: str):
        try:
            end = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            secs = int((end - now).total_seconds())
        except Exception:
            return "", None
        if secs <= 0:
            return "Expired", secs
        d, r = divmod(secs, 86400); h, r = divmod(r, 3600); m = r // 60
        return (f"{d}d " if d else "") + (f"{h}h " if (d or h) else "") + f"{m}m", secs

    groups = []
    total_extractors = 0
    expiring_soon = 0
    needs_relogin: list[str] = []
    for cid, res in results:
        cname = char_name.get(cid, str(cid))
        if res == "forbidden":
            needs_relogin.append(cname); continue
        if not res:
            continue
        colonies, details = res
        det = {c["planet_id"]: (d if isinstance(d, dict) else None)
               for c, d in zip(colonies, details)}
        col_list = []
        for c in colonies:
            d = det.get(c["planet_id"])
            extractors = []
            factory_count: dict[int, int] = {}  # schematic_id → number of factory pins
            stored_agg: dict[int, int] = {}     # aggregated storage/launchpad contents
            if d:
                for pin in d.get("pins", []):
                    ed = pin.get("extractor_details")
                    if ed:
                        exp = pin.get("expiry_time") or ""
                        rem, secs = _rem(exp) if exp else ("", None)
                        state = "expired" if (secs is not None and secs <= 0) else \
                                ("soon" if (secs is not None and secs < 86400) else "ok")
                        if state in ("expired", "soon"):
                            expiring_soon += 1
                        extractors.append({
                            "product": type_names.get(ed.get("product_type_id"), f"#{ed.get('product_type_id')}"),
                            "product_id": ed.get("product_type_id"),
                            "expiry_iso": exp,
                            "remaining": rem,
                            "state": state,
                            "qty_per_cycle": ed.get("qty_per_cycle", 0),
                            "cycle_hours": round((ed.get("cycle_time") or 0) / 3600, 1),
                            "heads": len(ed.get("heads", [])),
                        })
                    sid = pin.get("schematic_id")
                    if sid:
                        factory_count[sid] = factory_count.get(sid, 0) + 1
                    for cont in (pin.get("contents") or []):
                        tid = cont.get("type_id")
                        if tid:
                            stored_agg[tid] = stored_agg.get(tid, 0) + (cont.get("amount") or 0)

            # Production chains (output ← inputs, per schematic) + est. value/day.
            production = []
            value_day = 0.0
            for sid, cnt in factory_count.items():
                sc = schematics.get(sid)
                if not sc:
                    continue
                cyc = sc["cycle_time"] or 0
                per_day = (86400 / cyc) if cyc else 0
                out_price = price_map.get(sc["output_id"])
                if out_price and per_day:
                    value_day += out_price * sc["output_qty"] * cnt * per_day
                production.append({
                    "output": type_names.get(sc["output_id"], f"#{sc['output_id']}"),
                    "output_id": sc["output_id"],
                    "output_qty": sc["output_qty"],
                    "count": cnt,
                    "cycle_hours": round(cyc / 3600, 1) if cyc else 0,
                    "inputs": [{"name": type_names.get(i["type_id"], f"#{i['type_id']}"),
                                "type_id": i["type_id"], "qty": i["qty"]} for i in sc["inputs"]],
                })
            production.sort(key=lambda p: p["output"])
            # Extractor-only colony → value the raw extraction/day instead (avoids
            # double-counting P0 that a factory would consume).
            if not production:
                for e in extractors:
                    p0 = price_map.get(e["product_id"])
                    if p0 and e["cycle_hours"]:
                        value_day += p0 * e["qty_per_cycle"] * (24 / e["cycle_hours"])

            stored = sorted(
                ({"name": type_names.get(tid, f"#{tid}"), "type_id": tid, "amount": amt}
                 for tid, amt in stored_agg.items() if amt),
                key=lambda x: -x["amount"])
            total_extractors += len(extractors)
            soonest = min((e["expiry_iso"] for e in extractors if e["expiry_iso"]), default="")
            col_list.append({
                "planet_id": c["planet_id"],
                "planet_name": planet_names.get(c["planet_id"], f"Planet #{c['planet_id']}"),
                "system": "",   # the planet name already includes the system
                "type_label": planets_api.planet_type_label(c.get("planet_type", "")),
                "planet_type": c.get("planet_type", ""),
                "upgrade_level": c.get("upgrade_level", 0),
                "num_pins": c.get("num_pins", 0),
                "extractors": extractors,
                "production": production,
                "value_day": round(value_day) if value_day else 0,
                "stored": stored,
                "soonest_iso": soonest,
            })
        col_list.sort(key=lambda x: x["soonest_iso"] or "9999")
        groups.append({"char_id": cid, "char_name": cname, "colonies": col_list})

    groups.sort(key=lambda g: (g["colonies"][0]["soonest_iso"] if g["colonies"] else "9999"))

    # Refresh the PI alert cache from this (freshest) view so the dashboard tile
    # + nav badge reflect it. Per-char, only for characters we fetched OK.
    try:
        ok_cids = [cid for cid, res in results if res and not isinstance(res, str)]
        entries = [{
            "char_id": g["char_id"], "char_name": g["char_name"],
            "planet_id": col["planet_id"], "planet_name": col["planet_name"],
            "product_id": e["product_id"], "product": e["product"], "expiry_iso": e["expiry_iso"],
        } for g in groups for col in g["colonies"] for e in col["extractors"] if e.get("expiry_iso")]
        _store_pi_cache_for_chars(conn, ok_cids, entries)
    except Exception as exc:
        print(f"[planets] pi-cache update failed: {exc}", flush=True)

    conn.close()
    return _tr("planets.html", request, {
        "groups": groups, "error": None,
        "total_extractors": total_extractors, "expiring_soon": expiring_soon,
        "needs_relogin": needs_relogin,
    })


@router.get("/pi-planner", response_class=HTMLResponse)
async def pi_planner_page(
    request: Request,
    target: str = "",
    qty: str = "",
    period: str = "",
    derate: str = "",
    ccu: str = "",
    me: str = "",
):
    """PI planner — works backwards from a target product to colony counts,
    then cross-references the plan against the character's live colonies.

    The plan itself is static SDE maths (app/planetary/); the "plan vs actual"
    half reuses the same PI fetch /planets does, so it costs no extra ESI calls.
    """
    view = pi_planner_helper.build_view_model(
        database_path(),
        target=target,
        quantity=pi_planner_helper.parse_quantity(qty),
        period=pi_planner_helper.parse_period(period),
        derate=pi_planner_helper.parse_derate(derate),
        ccu_level=pi_planner_helper.parse_ccu_level(ccu),
        me=pi_planner_helper.parse_me(me),
    )
    if view["result"]:
        conn = get_conn()
        try:
            chars = list_characters(conn)
            results = await _fetch_pi_colonies(conn, chars) if chars else []
            # Shared name cache — the same resolver /planets uses, so a planet
            # either page has seen costs nothing here.
            planet_ids = {c["planet_id"]
                          for _cid, res in results if res and not isinstance(res, str)
                          for c in res[0]}
            planet_names = await _resolve_planet_names(conn, planet_ids) if planet_ids else {}
            view["actual"] = pi_planner_helper.build_plan_vs_actual(
                conn, results, dict(chars), view["result"]["colonies"],
                planet_names=planet_names)
            view["signed_in"] = bool(chars)
            # Refresh the shared PI alert cache from this (freshest) view, exactly
            # as /planets does — same store, same per-character replacement, so
            # the dashboard tile and nav badge stay current either way.
            try:
                _store_pi_cache_for_chars(
                    conn, view["actual"]["ok_char_ids"], view["actual"]["cache_entries"])
            except Exception as exc:
                print(f"[pi-planner] pi-cache update failed: {exc}", flush=True)
        finally:
            conn.close()
    return _tr("pi_planner.html", request, view)


# ── PI extractor alerts (dashboard tile + nav badge) ─────────────────────────
# PI is "set and forget until the extractor runs out", so the useful alert is
# "which extractors expire within 24h (or already have)". We cache the extractor
# expiry times in the DB so the count can be shown cheaply on every page (nav
# badge) without hitting ESI; the dashboard tile refreshes the cache live.

_PI_CACHE_TTL = 900.0   # 15 min — extractor programs run for days, so this is plenty


def _ensure_pi_cache_tables(conn: sqlite3.Connection) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists."""
    ensure_db_schema(conn)


async def _resolve_planet_names(conn: sqlite3.Connection, planet_ids) -> dict[int, str]:
    """Planet names ("Jita IV" — includes the system). Cached permanently in the
    DB (they never change); only cache-misses hit ESI's per-planet endpoint
    (/universe/names can't resolve planets)."""
    _ensure_pi_cache_tables(conn)
    names: dict[int, str] = {}
    miss: list[int] = []
    for pid in planet_ids:
        row = conn.execute("SELECT name FROM planet_name_cache WHERE planet_id=?", (pid,)).fetchone()
        if row and row[0]:
            names[pid] = row[0]
        else:
            miss.append(pid)
    if miss:
        async def _p(client, pid):
            try:
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/planets/{pid}/",
                    params={"datasource": "tranquility"}, timeout=8)
                if r.status_code == 200:
                    return pid, r.json().get("name")
            except Exception:
                pass
            return pid, None
        try:
            async with esi_client(timeout=8) as client:
                for pid, nm in await asyncio.gather(*[_p(client, p) for p in miss]):
                    if nm:
                        names[pid] = nm
                        conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?) ON CONFLICT (planet_id) DO UPDATE SET name=excluded.name", (pid, nm))
            conn.commit()
        except Exception:
            pass
    return names


def _store_pi_cache_for_chars(conn: sqlite3.Connection, char_ids, entries) -> None:
    """Replace the cached extractors for the given characters (per-char, so a
    character whose ESI fetch failed keeps its last-known rows)."""
    _ensure_pi_cache_tables(conn)
    if not char_ids:
        return
    ph = ",".join("?" * len(char_ids))
    conn.execute(f"DELETE FROM pi_extractor_cache WHERE char_id IN ({ph})", list(char_ids))
    if entries:
        now = _time.time()
        conn.executemany(
            "INSERT INTO pi_extractor_cache "
            "(char_id,char_name,planet_id,planet_name,product_id,product,expiry_iso,cached_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (char_id, planet_id, product_id) DO UPDATE SET char_name=excluded.char_name, planet_name=excluded.planet_name, product=excluded.product, expiry_iso=excluded.expiry_iso, cached_at=excluded.cached_at",
            [(e["char_id"], e["char_name"], e["planet_id"], e["planet_name"],
              e["product_id"], e["product"], e["expiry_iso"], now) for e in entries])
    conn.commit()


async def _pi_fetch_and_cache(conn: sqlite3.Connection, chars) -> None:
    """Fetch every character's colonies + extractor expiry times and refresh the
    PI cache. Lightweight vs the full /planets view (extractors only)."""
    async def _one(cid: int):
        tok = _get_valid_token_for(conn, cid)
        if not tok:
            return cid, None
        try:
            async with esi_client() as client:
                colonies = await planets_api.fetch_planets(client, cid, tok)
                if colonies == "forbidden" or colonies is None:
                    return cid, colonies
                if not colonies:
                    return cid, ([], [])
                details = await asyncio.gather(*[
                    planets_api.fetch_planet_detail(client, cid, c["planet_id"], tok)
                    for c in colonies], return_exceptions=True)
                return cid, (colonies, details)
        except Exception:
            return cid, None

    results = await asyncio.gather(*[_one(cid) for cid, _ in chars])
    char_name = {cid: name for cid, name in chars}

    type_ids: set[int] = set()
    planet_ids: set[int] = set()
    raw: list[tuple] = []   # (cid, planet_id, product_id, expiry_iso)
    ok_cids: list[int] = []
    for cid, res in results:
        if res is None or res == "forbidden":
            continue
        ok_cids.append(cid)
        colonies, details = res
        det = {c["planet_id"]: (d if isinstance(d, dict) else None)
               for c, d in zip(colonies, details)}
        for c in colonies:
            planet_ids.add(c["planet_id"])
            d = det.get(c["planet_id"])
            if not d:
                continue
            for pin in d.get("pins", []):
                ed = pin.get("extractor_details")
                if ed and pin.get("expiry_time") and ed.get("product_type_id"):
                    raw.append((cid, c["planet_id"], ed["product_type_id"], pin["expiry_time"]))
                    type_ids.add(ed["product_type_id"])

    type_names: dict[int, str] = {}
    if type_ids:
        ph = ",".join("?" * len(type_ids))
        type_names = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(type_ids))}
    planet_names = await _resolve_planet_names(conn, planet_ids)

    entries = [{
        "char_id": cid, "char_name": char_name.get(cid, str(cid)),
        "planet_id": pid, "planet_name": planet_names.get(pid, f"Planet #{pid}"),
        "product_id": prod, "product": type_names.get(prod, f"#{prod}"),
        "expiry_iso": exp,
    } for cid, pid, prod, exp in raw]
    _store_pi_cache_for_chars(conn, ok_cids, entries)


def _pi_alert_summary(conn: sqlite3.Connection, limit: int = 8) -> dict:
    """Read the PI cache and compute, against the CURRENT time, how many
    extractors expire within 24h / are already expired, plus the soonest few."""
    _ensure_pi_cache_tables(conn)
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = conn.execute(
        "SELECT char_name, planet_name, product, expiry_iso FROM pi_extractor_cache"
    ).fetchall()
    # Age of the cache itself, so callers can decide whether an ESI refresh is
    # worth it (None = nothing cached yet).
    _age_row = conn.execute("SELECT MAX(cached_at) FROM pi_extractor_cache").fetchone()
    cache_age = (_time.time() - _age_row[0]) if (_age_row and _age_row[0]) else None
    items = []
    n_soon = n_expired = 0
    for cname, pname, prod, iso in rows:
        try:
            end = _dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
            if end.tzinfo is None:            # be lenient: treat naive as UTC
                end = end.replace(tzinfo=_dt.timezone.utc)
            secs = int((end - now).total_seconds())
        except Exception:
            continue
        if secs <= 0:
            state = "expired"; n_expired += 1
        elif secs < 86400:
            state = "soon"; n_soon += 1
        else:
            state = "ok"
        items.append({"char": cname, "planet": pname, "product": prod,
                      "expiry_iso": iso, "secs": secs, "state": state})
    items.sort(key=lambda x: x["secs"])
    alerts = [i for i in items if i["state"] in ("expired", "soon")]
    return {
        "n_soon": n_soon, "n_expired": n_expired, "n_alert": n_soon + n_expired,
        "total": len(items),
        "items": alerts[:limit] if limit else [],
        "soonest_secs": items[0]["secs"] if items else None,
        "age": cache_age,
    }


@router.get("/api/dashboard/pi-alerts")
async def api_pi_alerts(force: int = 0):
    """Alert summary for the dashboard tile. Cache-first: a live refresh costs
    one colony-list call per character plus one detail call per planet (80+ ESI
    calls on a 12-character account), and it used to run on EVERY dashboard
    load. Extractor programs last days, so serving a cache younger than
    _PI_CACHE_TTL is just as accurate — the countdowns are computed against the
    current time anyway. `force=1` refreshes regardless."""
    conn = get_conn()
    try:
        chars = list_characters(conn)
        summary = _pi_alert_summary(conn)
        age = summary.get("age")
        fresh = (age is not None and age < _PI_CACHE_TTL)
        if chars and (force or not fresh):
            try:
                await _pi_fetch_and_cache(conn, chars)
                summary = _pi_alert_summary(conn)
            except Exception as exc:
                print(f"[pi-alerts] refresh failed: {exc}", flush=True)
        else:
            summary["from_cache"] = True
        return summary
    finally:
        conn.close()


@router.get("/api/pi-alert-count")
async def api_pi_alert_count():
    """Cheap cache-only alert count (no ESI). Used by the nav badge on every page."""
    conn = get_conn()
    try:
        return _pi_alert_summary(conn, limit=0)
    finally:
        conn.close()
