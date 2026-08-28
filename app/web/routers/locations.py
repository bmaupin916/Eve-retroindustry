"""Stations, structures and the lookups the plan form hangs off.

Moved out of `main.py` unchanged (W6). What ties these together is the
location: naming one, resolving one, rigging one, finding what is near one, and
pricing an item at one. `/api/suggest` is here for the same reason — it answers
"what do I own, and where", which is a question about places as much as items.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request

from sqlalchemy import bindparam, text

from app.db.conn import connect
from app.esi.client import esi_client
from app.market.prices import (
    ensure_price_table,
    fetch_station_volumes,
    fetch_structure_market,
)
from app.web import contracts_helper
from app.db.conn import connect as _connect
from app.web.deps import (
    _load_assets_from_cache,
    _load_blueprints_from_cache,
    get_active_character,
    get_active_token,
    get_conn,
    ensure_schema,
)
from app.web.industry_helper import (
    get_rig_types,
    get_sci_for_system,
    get_station_me_bonus_pct,
    get_station_rigs_full,
    populate_rig_bonuses,
    save_station_rigs_full,
)
from app.web.location_resolver import (
    get_region_for_location,
    get_security_status,
    load_location_names_from_db,
    locations_in_system,
    resolve_station_names_bulk,
)

router = APIRouter()


# ── cache access from an unconverted router ──────────────────────────────────
# `location_resolver` is on the portable query layer; this router still holds a
# raw sqlite3 handle from `get_conn()`. Each of these opens its own engine
# connection and closes it immediately. They exist as helpers rather than inline
# `with` blocks because `suggest_station` touches the cache six times across a
# 140-line body, and wrapping it would reindent a handler this change has no
# other reason to touch.

async def _resolve_names(ids, token):
    with connect() as c:
        return await resolve_station_names_bulk(ids, token=token, conn=c)


def _names_from_db() -> dict[int, str]:
    with connect() as c:
        return load_location_names_from_db(c)


def _locations_in_system(solar_system_id: int) -> list[dict]:
    with connect() as c:
        return locations_in_system(c, solar_system_id)


async def _region_for(location_id: int, token: str | None) -> int | None:
    with connect() as c:
        return await get_region_for_location(c, location_id, token)



@router.get("/api/station-industry-info")
async def station_industry_info(request: Request, location_id: int):
    """
    Return SCI, facility tax, ME bonus and security multiplier for the given station/structure.
    Facility tax is derived from the character's recent jobs (cost/EIV − SCI).
    """
    with connect() as conn:
        sys_row = conn.execute(
            text("SELECT solar_system_id FROM location_name_cache"
                 " WHERE location_id=:loc"),
            {"loc": location_id},
        ).fetchone()
        solar_system_id: int | None = sys_row[0] if sys_row and sys_row[0] else None

        mfg_sci = rxn_sci = 0.0
        security_status: float | None = None
        if solar_system_id:
            mfg_sci = await get_sci_for_system(conn, solar_system_id, "manufacturing")
            rxn_sci = await get_sci_for_system(conn, solar_system_id, "reaction")
            # Pre-fetch security_status into the cache so the synchronous helper
            # get_station_me_bonus_pct can scale rig bonuses correctly (×1.0 / ×1.9 / ×2.1).
            security_status = await get_security_status(conn, solar_system_id)

        # We can't read facility tax exactly from ESI (deriving it from the job average was inaccurate).
        # The user enters it manually and can save the value as a default (localStorage).
        rig_info = get_station_rigs_full(conn, location_id)
        # ME bonus recomputed with the security multiplier (overrides the stale stored value)
        me_bonus_live = get_station_me_bonus_pct(conn, location_id)
    return {
        "solar_system_id":  solar_system_id,
        "security_status":  security_status,
        "mfg_sci":          mfg_sci,
        "rxn_sci":          rxn_sci,
        "me_bonus_pct":     me_bonus_live,
        "structure_type":   rig_info["structure_type"],
        "rigs":             rig_info["rigs"],
    }


@router.post("/api/station-rigs")
async def save_station_rigs(request: Request):
    """Save the rig configuration for the given station/structure."""
    try:
        data = await request.json()
        location_id = int(data.get("location_id", 0))
        if not location_id:
            return {"ok": False, "error": "missing location_id"}
        structure_type = data.get("structure_type") or None
        rig1 = int(data["rig1_type_id"]) if data.get("rig1_type_id") else None
        rig2 = int(data["rig2_type_id"]) if data.get("rig2_type_id") else None
        rig3 = int(data["rig3_type_id"]) if data.get("rig3_type_id") else None
        with connect() as conn:
            save_station_rigs_full(conn, location_id, structure_type,
                                   rig1, rig2, rig3)
            # Return the security-adjusted ME bonus (the helper applies the sec
            # multiplier to rigs)
            me_bonus = get_station_me_bonus_pct(conn, location_id)
        return {"ok": True, "me_bonus_pct": me_bonus}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/rig-types")
async def api_rig_types(structure_type: str = ""):
    """Return the available rigs for the given structure type (raitaru/azbel/sotiyo/athanor/tatara)."""
    with connect() as conn:
        populate_rig_bonuses(conn)
        rigs = get_rig_types(conn, structure_type)
    return {"rigs": rigs}


@router.get("/api/suggest-station")
async def suggest_station(request: Request, q: str = ""):
    if len(q.strip()) < 2:
        return {"owned": [], "other": []}

    conn = get_conn()
    ensure_schema(conn)
    char = get_active_character(request)
    token = get_active_token(request)
    pattern = q.strip().lower()

    # Locations where the character has assets (personal + corporate)
    asset_locs: set[int] = set()
    if char:
        with _connect() as _ac:
            raw = _load_assets_from_cache(_ac, char[0])
        for a in raw:
            if not a.get("is_singleton", False):
                asset_locs.add(a["location_id"])

    all_names = _names_from_db()
    cache_empty = len(all_names) == 0

    # Stations with assets — filter by name
    owned_ids: set[int] = set()
    owned = []
    for loc_id in asset_locs:
        name = all_names.get(loc_id, str(loc_id))
        if pattern in name.lower() or pattern in str(loc_id):
            owned.append({"location_id": loc_id, "name": name})
            owned_ids.add(loc_id)
    owned.sort(key=lambda x: x["name"])

    # Other known stations from the cache without assets
    other = []
    other_ids: set[int] = set()
    for loc_id, name in all_names.items():
        if loc_id not in asset_locs and (pattern in name.lower() or pattern in str(loc_id)):
            other.append({"location_id": loc_id, "name": name})
            other_ids.add(loc_id)
    other.sort(key=lambda x: x["name"])

    # ESI lookup — NPC stations + systems + player structures (in parallel)
    try:
        async with esi_client() as client:
            # **CCP removed the public `/search/` endpoint.** It answers 404 on
            # every version for every query, including ones that cannot fail.
            # That silently cost this box two of its four sources: NPC stations
            # by name, and the system-name lookup everything below keys off.
            # Losing the second is the worse half — without a system id the code
            # never reaches "structures in this system" or "NPC stations in this
            # system", so a nullsec system typed in full returned nothing even
            # when it has an NPC station.
            #
            # It failed invisibly for two compounding reasons: a 404 here yields
            # an empty list rather than an error, and the *authenticated*
            # `/characters/{id}/search/` still exists — so player structures the
            # character can dock at kept appearing and the box looked alive.
            #
            # `POST /universe/ids/` is the documented replacement. It matches
            # **whole names only**: the old `strict=false` partial matching is
            # gone for good. The local-cache pass above is what still does
            # substring matching, over every name already known — so once
            # "PR-8CA" has been looked up once, typing "PR-8" finds it again.
            esi_tasks: list = [
                client.post(
                    "https://esi.evetech.net/latest/universe/ids/",
                    params={"datasource": "tranquility"},
                    json=[q.strip()],
                    timeout=5.0,
                ),
            ]
            # Authenticated search for player structures (citadels, engineering complexes…)
            if char and token:
                esi_tasks.append(
                    client.get(
                        f"https://esi.evetech.net/latest/characters/{char[0]}/search/",
                        params={"categories": "structure", "search": q.strip(),
                                "datasource": "tranquility", "strict": "false"},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=5.0,
                    )
                )

            results = await asyncio.gather(*esi_tasks, return_exceptions=True)
            ids_res = results[0]
            structure_search = results[1] if len(results) > 1 else None

            # A name matching nothing comes back as an empty object with a 200,
            # so there is no error case to tell apart from "no such name".
            npc_ids: list[int] = []
            system_ids: list[int] = []
            if not isinstance(ids_res, Exception) and ids_res.status_code == 200:
                payload = ids_res.json() or {}
                npc_ids = [e["id"] for e in (payload.get("stations") or [])][:20]
                system_ids = [e["id"] for e in (payload.get("systems") or [])]

            # NPC stations named outright
            if npc_ids:
                new_ids = [sid for sid in npc_ids if sid not in all_names]
                if new_ids:
                    new_names = await _resolve_names(new_ids, None)
                    all_names.update(new_names)
                for sid in npc_ids:
                    if sid in asset_locs and sid not in owned_ids:
                        owned.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        owned_ids.add(sid)
                    elif sid not in asset_locs and sid not in other_ids:
                        other.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        other_ids.add(sid)

            # Player structures — result from the authenticated character search
            if (structure_search and not isinstance(structure_search, Exception)
                    and structure_search.status_code == 200):
                struct_ids = structure_search.json().get("structure", [])[:20]
                new_struct_ids = [sid for sid in struct_ids if sid not in all_names]
                if new_struct_ids:
                    new_names = await _resolve_names(new_struct_ids, token)
                    all_names.update(new_names)
                for sid in struct_ids:
                    if sid in asset_locs and sid not in owned_ids:
                        owned.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        owned_ids.add(sid)
                    elif sid not in asset_locs and sid not in other_ids:
                        other.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        other_ids.add(sid)

            # Systems — find structures in our cache + NPC stations in the system
            for sys_id in system_ids[:10]:
                for entry in _locations_in_system(sys_id):
                    lid = entry["location_id"]
                    if lid in asset_locs and lid not in owned_ids:
                        owned.append(entry)
                        owned_ids.add(lid)
                    elif lid not in asset_locs and lid not in other_ids:
                        other.append(entry)
                        other_ids.add(lid)

            # NPC stations in the found systems
            sys_tasks = [
                client.get(
                    f"https://esi.evetech.net/latest/universe/systems/{sid}/",
                    params={"datasource": "tranquility"}, timeout=4.0,
                )
                for sid in system_ids[:5]
            ]
            if sys_tasks:
                sys_results = await asyncio.gather(*sys_tasks, return_exceptions=True)
                new_npc: list[int] = []
                for sys_r in sys_results:
                    if not isinstance(sys_r, Exception) and sys_r.status_code == 200:
                        new_npc.extend(sys_r.json().get("stations", []))
                new_npc_ids = [sid for sid in new_npc if sid not in all_names]
                if new_npc_ids:
                    new_names = await _resolve_names(new_npc_ids, None)
                    all_names.update(new_names)
                for sid in new_npc:
                    if sid not in asset_locs and sid not in other_ids:
                        other.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        other_ids.add(sid)

            other.sort(key=lambda x: x["name"])
            owned.sort(key=lambda x: x["name"])
    except Exception:
        pass

    conn.close()
    return {"owned": owned[:15], "other": other[:10], "cache_empty": cache_empty and not owned and not other}


@router.post("/api/add-station")
async def add_station(request: Request, raw: str = Form(...)):
    """
    Add a structure to the cache. Accepts:
    - structure ID (a number)
    - EVE URL format: <url=showinfo:TYPE//ID>Name</url>
    - ID<space>Name: e.g. "1045667241057 C-N4OD - Fortizar"
    """
    import re
    conn = get_conn()
    ensure_schema(conn)
    token = get_active_token(request)

    raw = raw.strip()
    structure_id: int | None = None
    hint_name: str | None = None

    # EVE URL format: showinfo:TYPE//ID or showinfo:TYPE//ID>Name
    m = re.search(r'showinfo:\d+//(\d+)(?:[^>]*>([^<]+))?', raw)
    if m:
        structure_id = int(m.group(1))
        hint_name = m.group(2).strip() if m.group(2) else None
    # Just a number, or "ID name"
    elif raw:
        parts = raw.split(None, 1)
        if parts[0].isdigit():
            structure_id = int(parts[0])
            hint_name = parts[1].strip() if len(parts) > 1 else None

    if not structure_id:
        return {"error": "Could not recognize the structure ID"}, 400

    resolved_name = hint_name
    sys_id: int | None = None

    # Try ESI
    try:
        async with esi_client() as client:
            if structure_id < 1_000_000_000_000:
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/stations/{structure_id}/",
                    params={"datasource": "tranquility"}, timeout=8,
                )
            else:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/structures/{structure_id}/",
                    params={"datasource": "tranquility"}, headers=headers, timeout=8,
                )
            if r.status_code == 200:
                data = r.json()
                resolved_name = data.get("name") or resolved_name
                sys_id = data.get("solar_system_id") or data.get("system_id")
    except Exception:
        pass

    if not resolved_name:
        resolved_name = f"[Structure {structure_id}]"

    with _connect() as _lc:
        _lc.execute(
            text("INSERT INTO location_name_cache (location_id, name, solar_system_id)"
                 " VALUES (:location_id, :name, :solar_system_id)"
                 " ON CONFLICT (location_id) DO UPDATE SET"
                 " name=excluded.name, solar_system_id=excluded.solar_system_id"),
            {"location_id": structure_id, "name": resolved_name,
             "solar_system_id": sys_id},
        )
        _lc.commit()
    conn.close()
    return {"location_id": structure_id, "name": resolved_name, "solar_system_id": sys_id}


@router.post("/api/location/rename")
async def location_rename(request: Request):
    """Save a user-entered location name to the cache."""
    body = await request.json()
    location_id = int(body["location_id"])
    name = str(body.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "Empty name"}
    conn = get_conn()
    ensure_schema(conn)
    with _connect() as _lc:
        _lc.execute(
            text("INSERT INTO location_name_cache (location_id, name)"
                 " VALUES (:location_id, :name)"
                 " ON CONFLICT (location_id) DO UPDATE SET name=excluded.name"),
            {"location_id": location_id, "name": name},
        )
        _lc.commit()
    conn.close()
    from app.web.location_resolver import _cache
    _cache[location_id] = name
    return {"ok": True, "location_id": location_id, "name": name}


@router.get("/api/location/resolve")
async def location_resolve(request: Request, location_id: int):
    """Try to look up the structure name via ESI with the current token."""
    conn = get_conn()
    token = get_active_token(request)
    if not token:
        conn.close()
        return {"ok": False, "error": "Not signed in"}
    from app.web.location_resolver import resolve_station_name, _cache
    _cache.pop(location_id, None)  # force a fresh ESI call
    async with esi_client() as client:
        name, sys_id = await resolve_station_name(client, location_id, token)
    resolved = name != str(location_id) and not name.startswith("[")
    if resolved:
        ensure_schema(conn)
        with _connect() as _lc:
            _lc.execute(
                text("INSERT INTO location_name_cache (location_id, name, solar_system_id)"
                     " VALUES (:location_id, :name, :solar_system_id)"
                     " ON CONFLICT (location_id) DO UPDATE SET"
                     " name=excluded.name, solar_system_id=excluded.solar_system_id"),
                {"location_id": location_id, "name": name,
                 "solar_system_id": sys_id},
            )
            _lc.commit()
    conn.close()
    return {"ok": resolved, "name": name, "solar_system_id": sys_id}


@router.get("/api/my-location")
async def my_location(request: Request):
    """Return the character's current location (structure_id if docked in a structure)."""
    conn = get_conn()
    token = get_active_token(request)
    char = get_active_character(request)
    if not token or not char:
        conn.close()
        return {"error": "Not signed in"}
    ensure_schema(conn)

    try:
        async with esi_client() as client:
            r = await client.get(
                f"https://esi.evetech.net/latest/characters/{char[0]}/location/",
                params={"datasource": "tranquility"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            if r.status_code != 200:
                return {"error": f"ESI {r.status_code}"}
            loc = r.json()

        structure_id: int | None = loc.get("structure_id") or loc.get("station_id")
        sys_id: int = loc.get("solar_system_id", 0)

        if not structure_id:
            # Load the system name
            async with esi_client() as client:
                sr = await client.get(
                    f"https://esi.evetech.net/latest/universe/systems/{sys_id}/",
                    params={"datasource": "tranquility"}, timeout=5,
                )
                sys_name = sr.json().get("name", str(sys_id)) if sr.status_code == 200 else str(sys_id)
            return {"in_space": True, "solar_system_id": sys_id, "solar_system_name": sys_name}

        # Resolve the structure/station name and save it to the cache
        resolved_name = str(structure_id)
        try:
            async with esi_client() as client:
                if structure_id < 1_000_000_000_000:
                    r2 = await client.get(
                        f"https://esi.evetech.net/latest/universe/stations/{structure_id}/",
                        params={"datasource": "tranquility"}, timeout=8,
                    )
                else:
                    r2 = await client.get(
                        f"https://esi.evetech.net/latest/universe/structures/{structure_id}/",
                        params={"datasource": "tranquility"},
                        headers={"Authorization": f"Bearer {token}"}, timeout=8,
                    )
                if r2.status_code == 200:
                    data = r2.json()
                    resolved_name = data.get("name", resolved_name)
                    sys_id = data.get("solar_system_id") or data.get("system_id") or sys_id
        except Exception:
            pass

        with _connect() as _lc:
            _lc.execute(
                text("INSERT INTO location_name_cache (location_id, name, solar_system_id)"
                     " VALUES (:location_id, :name, :solar_system_id)"
                     " ON CONFLICT (location_id) DO UPDATE SET"
                     " name=excluded.name, solar_system_id=excluded.solar_system_id"),
                {"location_id": structure_id, "name": resolved_name,
                 "solar_system_id": sys_id},
            )
            _lc.commit()
        conn.close()
        return {"location_id": structure_id, "name": resolved_name,
                "solar_system_id": sys_id, "in_space": False}
    except Exception as e:
        conn.close()
        return {"error": str(e)}


@router.get("/api/plan/fetch-sell-price")
async def fetch_plan_sell_price(request: Request, location_id: int, type_id: int):
    """Fetch the best sell price of a specific product at the given station, save it to station_volume_cache."""
    conn = get_conn()
    token = get_active_token(request)

    # Ensure the type_id is present in market_price_cache (the fetchers need it for filtering)
    with _connect() as _pc:
        _pc.execute(
            text("INSERT INTO market_price_cache"
                 " (type_id, sell_price, buy_price, cached_at)"
                 " VALUES (:type_id, NULL, NULL, 0)"
                 " ON CONFLICT (type_id) DO NOTHING"),
            {"type_id": type_id},
        )
        _pc.commit()
    conn.commit()

    region_id = await _region_for(location_id, token)

    try:
        # One engine connection for the whole fetch. `app/market/prices.py` is on
        # the portable layer now, and unlike the route-jump cache these writers
        # need the connection *during* the ESI calls — they persist orders and
        # volumes as the pages arrive — so it is held across the await by
        # necessity rather than by oversight.
        with _connect() as _mc:
            ensure_price_table(_mc)
            if location_id >= 1_000_000_000:
                if not token:
                    conn.close()
                    return {"ok": False, "error": "Sign-in is required to access the structure market."}
                result = await fetch_structure_market(_mc, location_id, token, {type_id}, region_id)
            else:
                if not region_id:
                    conn.close()
                    return {"ok": False, "error": "Could not determine the region for this location."}
                result = await fetch_station_volumes(_mc, location_id, region_id, [type_id])
    except PermissionError as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}

    conn.close()
    best_sell = result.get(type_id, (None, None, None))[1] if result else None
    return {"ok": True, "best_sell": best_sell}


@router.get("/api/plan/contract-price")
async def api_plan_contract_price(request: Request, location_id: int, type_id: int):
    """Cheapest price per unit of the product from indexed public contracts in
    the given station's region. Requires the region to have been indexed first (Public browser)."""
    conn = get_conn()
    try:
        token = get_active_token(request)
        region_id = await _region_for(location_id, token)
        if not region_id:
            return {"ok": False, "error": "Could not determine the station's region."}
        # Its own connection: `contracts_helper` is on the portable query layer
        # while this router is not, so `conn` here is still a raw sqlite3
        # handle for the statements below. Same database either way, and both
        # of these come along free when this router converts.
        with _connect() as _ch:
            status = contracts_helper.get_index_status(_ch, region_id)
            if not status:
                return {"ok": False, "not_indexed": True, "region_id": region_id,
                        "error": "The contract region is not indexed — index it in the Public browser."}
            best = contracts_helper.best_contract_price(_ch, region_id, type_id)
        if not best:
            return {"ok": False, "error": "No public contract with this product in the region.",
                    "region_id": region_id, "indexed_at": status.get("indexed_at")}
        best["ok"] = True
        best["region_id"] = region_id
        best["indexed_at"] = status.get("indexed_at")   # so the client can re-index a stale index
        return best
    finally:
        conn.close()


@router.get("/api/suggest")
async def suggest(request: Request, q: str = ""):
    if len(q.strip()) < 2:
        return {"owned": [], "other": []}

    conn = get_conn()
    char = get_active_character(request)
    pattern = f"%{q.strip().lower()}%"
    owned: list[dict] = []
    owned_product_ids: set[int] = set()

    if char:
        char_id, _ = char
        with _connect() as _ac:
            raw_bps = _load_blueprints_from_cache(_ac, char_id)
        if raw_bps:
            bp_type_ids = list({bp["type_id"] for bp in raw_bps})
            # Group by type_id — pick the best one (BPO > BPC, highest ME)
            bp_by_type: dict[int, dict] = {}
            for bp in raw_bps:
                tid = bp["type_id"]
                if tid not in bp_by_type:
                    bp_by_type[tid] = bp
                else:
                    cur = bp_by_type[tid]
                    if (bp.get("quantity", 1) == -1, bp.get("material_efficiency", 0)) > \
                       (cur.get("quantity", 1) == -1, cur.get("material_efficiency", 0)):
                        bp_by_type[tid] = bp

            with _connect() as _sc:
                rows = _sc.execute(
                    text("""
                SELECT sbp.blueprint_type_id, sbp.product_type_id, t.name
                FROM sde_blueprint_products sbp
                JOIN sde_types t ON t.type_id = sbp.product_type_id
                WHERE sbp.blueprint_type_id IN :bp_ids
                  AND sbp.activity IN ('manufacturing', 'reaction')
                  AND LOWER(t.name) LIKE :pattern
                ORDER BY t.name
                    """).bindparams(bindparam("bp_ids", expanding=True)),
                    {"bp_ids": bp_type_ids, "pattern": pattern},
                ).fetchall()

            for bp_type_id, product_type_id, product_name in rows:
                owned_product_ids.add(product_type_id)
                bp = bp_by_type.get(bp_type_id, {})
                is_original = bp.get("quantity", 1) == -1
                runs = bp.get("runs", -1)
                owned.append({
                    "name": product_name,
                    "type_id": product_type_id,
                    "me": bp.get("material_efficiency", 0),
                    "te": bp.get("time_efficiency", 0),
                    "is_original": is_original,
                    "runs": "∞" if runs == -1 else runs,
                })

    # SDE — other blueprints (not owned)
    if owned_product_ids:
        with _connect() as _sc:
            other_rows = _sc.execute(
                text("""
            SELECT DISTINCT t.type_id, t.name
            FROM sde_types t
            JOIN sde_blueprint_products sbp ON sbp.product_type_id = t.type_id
            WHERE LOWER(t.name) LIKE :pattern
              AND sbp.activity IN ('manufacturing', 'reaction')
              AND t.type_id NOT IN :owned
            ORDER BY t.name LIMIT 15
                """).bindparams(bindparam("owned", expanding=True)),
                {"pattern": pattern, "owned": list(owned_product_ids)},
            ).fetchall()
    else:
        with _connect() as _sc:
            other_rows = _sc.execute(
                text("""
            SELECT DISTINCT t.type_id, t.name
            FROM sde_types t
            JOIN sde_blueprint_products sbp ON sbp.product_type_id = t.type_id
            WHERE LOWER(t.name) LIKE :pattern
              AND sbp.activity IN ('manufacturing', 'reaction')
            ORDER BY t.name LIMIT 15
                """),
                {"pattern": pattern},
            ).fetchall()

    conn.close()
    return {
        "owned": owned,
        "other": [{"name": r[1], "type_id": r[0]} for r in other_rows],
    }
