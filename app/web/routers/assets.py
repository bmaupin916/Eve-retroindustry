"""Assets and blueprints: the two per-character inventory pages.

Moved out of `main.py` unchanged (W6). The container and ship-hull machinery
comes with them — folding assembled ships into one row, pruning the tree by a
search term, resolving custom container names from ESI — because nothing else
uses it. `_container_display_name` is the exception and went to `deps.py`: the
dashboard labels the active ship with it.

The static jump-count cache belongs to `/api/assets/distances`, which is the
only caller.
"""
from __future__ import annotations

import asyncio
import time as _time

import httpx

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth.token_store import get_character_row, list_characters
from app.character.assets import (
    load_cached_assets,
    load_cached_container_names,
    load_cached_corp_assets,
)
from app.character.blueprints import load_cached_blueprints
from app.db.schema import ensure_schema as ensure_db_schema
from app.db.type_resolver import resolve_names_bulk
from app.esi.client import esi_client
from app.web.deps import (
    all_characters,
    any_character,
    character_row,
    _container_display_name,
    _valid_token_async,
    _load_assets_from_cache,
    _load_corp_assets_from_cache,
    _tr,
    get_active_character,
    get_active_token,
    get_conn,
)
from app.db.conn import connect as _connect, dbapi
from app.web.location_resolver import resolve_station_names_bulk
from app.web.prices_helper import get_prices_for_ids

router = APIRouter()


_CORP_DIV_LABEL: dict[str, str] = {
    "CorpSAG1": "Division 1",
    "CorpSAG2": "Division 2",
    "CorpSAG3": "Division 3",
    "CorpSAG4": "Division 4",
    "CorpSAG5": "Division 5",
    "CorpSAG6": "Division 6",
    "CorpSAG7": "Division 7",
    "Hangar": "Hangar",
    "CorpDeliveries": "Deliveries",
}
_CORP_DIV_ORDER = list(_CORP_DIV_LABEL.keys())


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

@router.get("/assets", response_class=HTMLResponse)
async def assets_page(request: Request, search: str = "", view: str = ""):
    conn = get_conn()
    all_chars = all_characters()
    stations: list[dict] = []
    corp_stations: list[dict] = []

    # Resolve which characters to load:
    #   view=all       → every char
    #   view=<id>      → that char
    #   view empty     → active char (cookie / first char)
    selected_chars: list[tuple[int, str]] = []
    if view == "all":
        selected_chars = list(all_chars)
    elif view.isdigit():
        cid = int(view)
        match = next((c for c in all_chars if c[0] == cid), None)
        if match:
            selected_chars = [match]
    if not selected_chars:
        active = get_active_character(request, conn)
        if active:
            selected_chars = [active]

    show_char_badge = view == "all" and len(all_chars) > 1

    # Per-char fetch (uses cache; ESI refresh only when stale)
    char_assets: dict[int, list] = {}            # char_id → personal assets list
    corp_data: dict[int, tuple[int, list]] = {}  # char_id → (corp_id, assets list)
    # item_ids of blueprint COPIES (BPCs) and ORIGINALS (BPOs). The assets
    # endpoint's is_blueprint_copy flag is unreliable (often missing), so we trust
    # the blueprints endpoint (authoritative BPO/BPC) — matched to assets by
    # item_id. all_bp_type_ids feeds reaction-formula detection below.
    bpc_item_ids: set[int] = set()
    bpo_item_ids: set[int] = set()
    all_bp_type_ids: set[int] = set()
    primary_token: str | None = None
    unsynced: list[str] = []
    oldest: list[float] = []
    if selected_chars:
        for cid, _name in selected_chars:
            tok = await _valid_token_async(cid)
            primary_token = primary_token or tok

            with _connect() as _ac:
                assets, at = load_cached_assets(_ac, cid)
                bps, _bat = load_cached_blueprints(_ac, cid)
            if assets is None:
                unsynced.append(_name or str(cid))
                char_assets[cid] = []
            else:
                char_assets[cid] = assets
                oldest.append(at)

            for bp in bps or []:
                (bpc_item_ids if not bp.is_original else bpo_item_ids).add(bp.item_id)
                all_bp_type_ids.add(bp.type_id)

            # The corporation id comes off the character row, which the sync
            # worker writes. `fetch_corp_assets` used to be the only way to get
            # it — and it asked ESI for it *before* consulting its own cache, so
            # even a cache hit cost a round trip on every page view.
            corp_id = (character_row(cid) or {}).get("corporation_id") or 0
            if corp_id:
                with _connect() as _ac:
                    corp_list, corp_at = load_cached_corp_assets(_ac, corp_id)
            else:
                corp_list, corp_at = None, 0.0
            corp_data[cid] = (corp_id, corp_list or [])
            if corp_list is not None:
                oldest.append(corp_at)

        all_type_ids_for_names = set()
        for assets in char_assets.values():
            all_type_ids_for_names |= {a.type_id for a in assets}
        for _, corp_list in corp_data.values():
            all_type_ids_for_names |= {a.type_id for a in corp_list}
        # No client: `resolve_names_bulk` reads the SDE and the name cache, and
        # only reaches ESI for ids neither knows. Passing None keeps it local.
        names = await resolve_names_bulk(conn, list(all_type_ids_for_names), None)
    else:
        names = {}

    if selected_chars:
        char_name_by_id = {cid: name for cid, name in all_chars}

        # Reaction-formula blueprint type_ids (reaction_time set, no manufacturing)
        # — so we can badge them "RXN" instead of "BPO".
        reaction_bp_types: set[int] = set()
        if all_bp_type_ids:
            with _connect() as _rc:
                reaction_bp_types = {
                    r[0] for r in _rc.execute(
                        text("SELECT blueprint_type_id FROM sde_blueprints"
                             " WHERE reaction_time > 0"
                             " AND blueprint_type_id IN :ids")
                        .bindparams(bindparam("ids", expanding=True)),
                        {"ids": list(all_bp_type_ids)},
                    ).fetchall()
                }

        def _bp_kind(a, is_copy: bool) -> str | None:
            """Badge kind for a blueprint asset: bpc / bpo / rxn (or None)."""
            if is_copy:
                return "bpc"
            if a.item_id in bpo_item_ids:
                return "rxn" if a.type_id in reaction_bp_types else "bpo"
            return None

        # ── Personal assets across all selected characters ────────────────
        station_data: dict[int, dict] = {}

        def _get_st(sid: int) -> dict:
            if sid not in station_data:
                station_data[sid] = {"hangar": {}, "containers": {}}
            return station_data[sid]

        # Build a per-char parent_map so container hierarchy resolves correctly
        for owner_id, assets_list in char_assets.items():
            parent_map = {a.item_id: a.location_id for a in assets_list}
            asset_item_ids = {a.item_id for a in assets_list}

            def _hierarchy(a, _items=asset_item_ids, _parents=parent_map) -> tuple[int, int | None]:
                loc = a.location_id
                if loc not in _items:
                    return loc, None
                container_id = loc
                cur = loc
                seen: set[int] = set()
                while cur in _items and cur not in seen:
                    seen.add(cur)
                    cur = _parents.get(cur, cur)
                    if cur not in _items:
                        break
                return cur, container_id

            owner_name = char_name_by_id.get(owner_id, "")
            for a in assets_list:
                item_name = names.get(a.type_id, f"Unknown ({a.type_id})")
                # NOT filtered here: `search` is applied once the tree is built,
                # by _prune_by_search. Dropping rows this early also dropped
                # everything inside a container, which is what left a searched-for
                # ship as a bare hull with nothing to expand.
                sid, cid = _hierarchy(a)
                st = _get_st(sid)
                bucket = st["hangar"] if cid is None else st["containers"].setdefault(cid, {})
                # BPC status from the authoritative blueprints endpoint (matched by
                # item_id), falling back to the asset flag. A BPC has no market price.
                is_copy = a.is_blueprint_copy or (a.item_id in bpc_item_ids)
                # Where it sits inside a ship (empty for hangar rows and plain
                # containers). Part of the key so the same module fitted in a slot
                # never merges with spares in cargo — that distinction IS the fit.
                slot, slot_order = _slot_info(a.location_flag) if cid is not None else ("", 0)
                # Key by (type_id, owner, is_copy, slot) so different chars stay
                # separate AND a BPO never merges with a BPC of the same type
                # (which would otherwise price the copy at the original's value).
                key = (a.type_id, owner_id, is_copy, slot)
                if key in bucket:
                    bucket[key]["quantity"] += a.quantity
                else:
                    bucket[key] = {
                        "type_id": a.type_id,
                        "name": item_name,
                        "quantity": a.quantity,
                        "is_blueprint_copy": is_copy,
                        "bp_kind": _bp_kind(a, is_copy),
                        "character_id": owner_id,
                        "character_name": owner_name,
                        "slot": slot,
                        "slot_order": slot_order,
                    }

        # Pick a primary char_id for legacy container-name lookups
        char_id = selected_chars[0][0]
        token = primary_token
        # corp_id / corp_assets_list — for single-char view, mirror legacy path;
        # for "all" mode, aggregate distinct corps
        if len(selected_chars) == 1:
            corp_id, corp_assets_list = corp_data.get(char_id, (0, []))
        else:
            corp_id = 0
            corp_assets_list = []
            seen_corp_ids: set[int] = set()
            for cid_corp, c_list in corp_data.values():
                if cid_corp and cid_corp not in seen_corp_ids:
                    seen_corp_ids.add(cid_corp)
                    corp_assets_list = corp_assets_list + c_list
            corp_id = next(iter(seen_corp_ids), 0)

        # ── Corporate assets ─────────────────────────────────────────────────
        # station_id → {div_flag → {"hangar": {type_id: item}, "containers": {cid: {type_id: item}}}}
        corp_sd: dict[int, dict] = {}
        if corp_assets_list:
            corp_item_ids = {a.item_id for a in corp_assets_list}
            corp_parent_map = {a.item_id: a.location_id for a in corp_assets_list}
            corp_flag_map = {a.item_id: a.location_flag for a in corp_assets_list}

            def _corp_hierarchy(a) -> tuple[int, str, int | None]:
                """Returns (station_id, division_flag, container_id|None).

                At NPC stations, items sit inside an office item (flag=OfficeFolder)
                and carry their own CorpSAG* flag. At citadels, items sit directly at
                the structure. In both cases we want the CorpSAG* flag as div_flag.
                """
                loc = a.location_id
                if loc not in corp_item_ids:
                    # Item directly at a station/citadel — its own flag IS the division
                    return loc, a.location_flag, None

                # Walk up the ownership chain to find the station
                chain: list[int] = []
                cur = loc
                seen: set[int] = set()
                while cur in corp_item_ids:
                    if cur in seen:
                        break
                    seen.add(cur)
                    chain.append(cur)
                    nxt = corp_parent_map.get(cur)
                    if nxt is None:
                        break
                    cur = nxt
                station_id = cur

                # Determine the division flag.
                # If the item itself carries a CorpSAG* flag it is directly in a
                # division (NPC office case) — use that flag and no container.
                if a.location_flag in _CORP_DIV_LABEL:
                    return station_id, a.location_flag, None

                # Item is inside a container — scan ancestors for a CorpSAG* flag
                div_flag = "Hangar"
                for ancestor_id in chain:
                    f = corp_flag_map.get(ancestor_id, "")
                    if f in _CORP_DIV_LABEL:
                        div_flag = f
                        break
                return station_id, div_flag, loc

            def _get_corp_div(sid: int, flag: str) -> dict:
                if sid not in corp_sd:
                    corp_sd[sid] = {}
                if flag not in corp_sd[sid]:
                    corp_sd[sid][flag] = {"hangar": {}, "containers": {}}
                return corp_sd[sid][flag]

            for a in corp_assets_list:
                if a.location_flag == "OfficeFolder":
                    continue  # office container itself — structural, not inventory
                item_name = names.get(a.type_id, f"Unknown ({a.type_id})")
                # Filtered later by _prune_by_search — see the personal loop above.
                sid, div_flag, cid = _corp_hierarchy(a)
                div = _get_corp_div(sid, div_flag)
                bucket = div["hangar"] if cid is None else div["containers"].setdefault(cid, {})
                # Keep BPO and BPC of the same type as separate rows (see personal
                # assets above). Corp blueprints aren't fetched, so the copy flag
                # here relies on the asset endpoint / any matched char BPC item_id.
                is_copy = a.is_blueprint_copy or (a.item_id in bpc_item_ids)
                slot, slot_order = _slot_info(a.location_flag) if cid is not None else ("", 0)
                ckey = (a.type_id, is_copy, slot)
                if ckey in bucket:
                    bucket[ckey]["quantity"] += a.quantity
                else:
                    bucket[ckey] = {
                        "type_id": a.type_id,
                        "name": item_name,
                        "quantity": a.quantity,
                        "is_blueprint_copy": is_copy,
                        # Corp blueprints aren't fetched, so we can only trust the
                        # (unreliable) copy flag here — badge BPC only, never guess BPO.
                        "bp_kind": "bpc" if is_copy else None,
                        "slot": slot,
                        "slot_order": slot_order,
                    }

        # ── Prices (shared for both personal and corp) ───────────────────────
        all_price_ids: set[int] = set()
        for sd in station_data.values():
            for bucket in [sd["hangar"], *sd["containers"].values()]:
                all_price_ids |= {item["type_id"] for item in bucket.values()}
        for sid_data in corp_sd.values():
            for dv in sid_data.values():
                for bucket in [dv["hangar"], *dv["containers"].values()]:
                    all_price_ids |= {item["type_id"] for item in bucket.values()}
        all_price_ids = list(all_price_ids)
        with _connect() as _pc:
            prices = await get_prices_for_ids(_pc, all_price_ids)

        def _add_prices(bucket: dict):
            for item in bucket.values():
                if item.get("is_blueprint_copy"):
                    item["unit_price"] = None
                    item["total_value"] = None
                else:
                    sell_p, _ = prices.get(item["type_id"], (None, None))
                    item["unit_price"] = sell_p
                    item["total_value"] = sell_p * item["quantity"] if sell_p else None

        for sd in station_data.values():
            _add_prices(sd["hangar"])
            for c in sd["containers"].values():
                _add_prices(c)

        for sid_data in corp_sd.values():
            for dv in sid_data.values():
                _add_prices(dv["hangar"])
                for c in dv["containers"].values():
                    _add_prices(c)

        # ── Location names ────────────────────────────────────────────────────
        all_loc_ids = list(set(station_data.keys()) | set(corp_sd.keys()))
        with _connect() as _lc:
            loc_names = await resolve_station_names_bulk(all_loc_ids, token, _lc)

        with _connect() as _sc:
            sys_rows = _sc.execute(text(
                "SELECT location_id, solar_system_id FROM location_name_cache"
                " WHERE solar_system_id IS NOT NULL")).fetchall()
        sys_map = {r[0]: r[1] for r in sys_rows}

        # ── Build personal stations ──────────────────────────────────────────
        all_container_ids = [cid for sd in station_data.values() for cid in sd["containers"]]

        # Aggregate assets_raw across all selected chars so container name
        # resolution works for every owner.
        # One connection for the whole comprehension rather than one per
        # character: the loader is on the portable layer now, and `_ac` has to
        # be bound somewhere — substituting the name without opening the block
        # is a NameError that only fires on a page with a character selected.
        with _connect() as _ac:
            assets_raw_by_char: dict[int, list] = {
                cid: _load_assets_from_cache(_ac, cid) for cid, _ in selected_chars
            }
        container_info: dict[int, tuple[str, int]] = {}
        if all_container_ids:
            for owner_id, _ in selected_chars:
                tok = await _valid_token_async(owner_id)
                if not tok:
                    continue
                owner_assets = assets_raw_by_char.get(owner_id, [])
                owner_info = await _resolve_container_names(
                    owner_id, tok, all_container_ids, owner_assets,
                )
                # First non-empty wins for each container
                for k, v in owner_info.items():
                    container_info.setdefault(k, v)
        container_type_map: dict[int, int] = {}
        # container_id → (owner_character_id, owner_character_name)
        char_name_lookup = {cid: name for cid, name in selected_chars}
        container_owner_map: dict[int, tuple[int, str]] = {}
        for owner_id, ar in assets_raw_by_char.items():
            for item in ar:
                container_type_map[item["item_id"]] = item["type_id"]
                container_owner_map.setdefault(
                    item["item_id"], (owner_id, char_name_lookup.get(owner_id, ""))
                )

        def _sort_items(bucket: dict) -> list:
            # Slot order first so a ship reads like the fitting window (hull, high,
            # mid, low, rigs, drones, cargo); hangar rows all share order 0 and so
            # stay purely alphabetical.
            return sorted(bucket.values(), key=lambda x: (x.get("slot_order") or 0, x["name"]))

        # Labels first (the search matches against them), then fold the hulls in,
        # then filter — a ship's own label has to exist before it can be matched.
        container_labels = {cid: info[0] for cid, info in container_info.items()}
        for sd in station_data.values():
            _fold_ship_hulls(sd, container_type_map, container_owner_map)
            _prune_by_search(sd, container_labels, search)

        for sid, sd in station_data.items():
            containers = []
            for cid, items in sd["containers"].items():
                cname = container_info.get(cid, (f"Container {cid}", sid))[0]
                owner = container_owner_map.get(cid)
                container_assets = _sort_items(items)
                c_value = sum(i.get("total_value") or 0 for i in container_assets)
                containers.append({
                    "container_id": cid,
                    "name": cname,
                    # Only a fitted ship has slot labels; a plain container has
                    # none, so the Slot column is skipped there rather than
                    # rendering a blank one. The folded hull row does not count —
                    # every folded container has one.
                    "has_slots": any(i.get("slot") for i in container_assets
                                     if not i.get("is_hull")),
                    "type_id": container_type_map.get(cid),
                    "assets": container_assets,
                    "total_value": c_value,
                    "character_id":   owner[0] if owner else None,
                    "character_name": owner[1] if owner else "",
                })
            containers.sort(key=lambda c: c["name"])
            hangar_items = _sort_items(sd["hangar"])
            # A search can empty a station out completely; don't render the header.
            if not hangar_items and not containers:
                continue
            hangar_value = sum(i.get("total_value") or 0 for i in hangar_items)
            containers_value = sum(c["total_value"] for c in containers)
            total_items = len(hangar_items) + sum(len(c["assets"]) for c in containers)
            # Pre-compute aggregates so the template doesn't re-run
            # `selectattr | map | sum` over the same lists multiple times
            # per station (was firing 4–6× in assets.html for big inventories).
            stations.append({
                "loc_id": sid,
                "name": loc_names.get(sid, str(sid)),
                "hangar": hangar_items,
                "hangar_value": hangar_value,
                "containers": containers,
                "containers_value": containers_value,
                "total_items": total_items,
                "total_value": hangar_value + containers_value,
                "solar_system_id": sys_map.get(sid),
            })

        stations.sort(key=lambda s: -s["total_items"])

        # ── Build corp stations ───────────────────────────────────────────────
        if corp_sd and corp_id:
            with _connect() as _ac:
                corp_assets_raw = _load_corp_assets_from_cache(_ac, corp_id)
            corp_container_type_map = {item["item_id"]: item["type_id"] for item in corp_assets_raw}
            all_corp_container_ids = [
                cid
                for sid_data in corp_sd.values()
                for dv in sid_data.values()
                for cid in dv["containers"]
            ]
            corp_container_info = await _resolve_corp_container_names(
                corp_id, token, all_corp_container_ids, corp_assets_raw
            ) if all_corp_container_ids else {}

            corp_container_labels = {
                cid: info[0] for cid, info in corp_container_info.items()
            }
            for sid_data in corp_sd.values():
                for dv in sid_data.values():
                    # No owner_map: corp rows carry no character.
                    _fold_ship_hulls(dv, corp_container_type_map)
                    _prune_by_search(dv, corp_container_labels, search)

            def _build_corp_container(cid, items, sid):
                container_assets = _sort_items(items)
                c_value = sum(i.get("total_value") or 0 for i in container_assets)
                return {
                    "container_id": cid,
                    "name": corp_container_info.get(cid, (f"Container {cid}", sid))[0],
                    "has_slots": any(i.get("slot") for i in container_assets
                                     if not i.get("is_hull")),
                    "type_id": corp_container_type_map.get(cid),
                    "assets": container_assets,
                    "total_value": c_value,
                }

            for sid, sid_data in corp_sd.items():
                divisions = []
                for flag in _CORP_DIV_ORDER:
                    if flag not in sid_data:
                        continue
                    dv = sid_data[flag]
                    containers = [
                        _build_corp_container(cid, items, sid)
                        for cid, items in dv["containers"].items()
                    ]
                    containers.sort(key=lambda c: c["name"])
                    hangar_items = _sort_items(dv["hangar"])
                    if not hangar_items and not containers:
                        continue
                    hangar_value = sum(i.get("total_value") or 0 for i in hangar_items)
                    containers_value = sum(c["total_value"] for c in containers)
                    divisions.append({
                        "flag": flag,
                        "label": _CORP_DIV_LABEL.get(flag, flag),
                        "hangar": hangar_items,
                        "hangar_value": hangar_value,
                        "containers": containers,
                        "containers_value": containers_value,
                        "total_value": hangar_value + containers_value,
                    })
                # Also include any flags not in _CORP_DIV_ORDER
                for flag, dv in sid_data.items():
                    if flag in _CORP_DIV_ORDER:
                        continue
                    hangar_items = _sort_items(dv["hangar"])
                    containers = sorted(
                        [_build_corp_container(cid, items, sid)
                         for cid, items in dv["containers"].items()],
                        key=lambda c: c["name"],
                    )
                    if hangar_items or containers:
                        hangar_value = sum(i.get("total_value") or 0 for i in hangar_items)
                        containers_value = sum(c["total_value"] for c in containers)
                        divisions.append({
                            "flag": flag,
                            "label": flag,
                            "hangar": hangar_items,
                            "hangar_value": hangar_value,
                            "containers": containers,
                            "containers_value": containers_value,
                            "total_value": hangar_value + containers_value,
                        })

                total_items = sum(
                    len(d["hangar"]) + sum(len(c["assets"]) for c in d["containers"])
                    for d in divisions
                )
                total_value = sum(d["total_value"] for d in divisions)
                # A search can prune every division away; skip the empty header.
                if not divisions:
                    continue
                corp_stations.append({
                    "loc_id": sid,
                    "name": loc_names.get(sid, str(sid)),
                    "divisions": divisions,
                    "total_items": total_items,
                    "total_value": total_value,
                    "solar_system_id": sys_map.get(sid),
                })

            corp_stations.sort(key=lambda s: -s["total_items"])

    conn.close()
    return _tr("assets.html", request, {
        "stations": stations,
        "corp_stations": corp_stations,
        "search": search,
        "view": view or "",
        "show_char_badge": show_char_badge,
        "selected_chars": selected_chars,
        # The oldest reading in the set, because that is the age of the weakest
        # part of the answer — the newest would describe whichever character
        # happened to sync last.
        "cached_at": min(oldest) if oldest else 0.0,
        "unsynced": unsynced,
    })


@router.get("/api/assets/distances")
async def assets_distances(request: Request):
    """Return the jump count from the character's current position to each location in assets."""
    conn = get_conn()
    char = get_active_character(request, conn)
    token = get_active_token(request, conn)
    if not char or not token:
        conn.close()
        return {"ok": False, "error": "Not signed in"}
    char_id, _ = char

    async with esi_client() as client:
        r = await client.get(
            f"https://esi.evetech.net/latest/characters/{char_id}/location/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    if r.status_code != 200:
        conn.close()
        return {"ok": False, "error": "Could not determine the character's location"}
    origin_sys = r.json().get("solar_system_id")
    if not origin_sys:
        conn.close()
        return {"ok": False, "error": "Character is not in a solar system"}

    with _connect() as _sc:
        rows = _sc.execute(text(
            "SELECT location_id, solar_system_id FROM location_name_cache"
            " WHERE solar_system_id IS NOT NULL")).fetchall()
    loc_to_sys = {row[0]: row[1] for row in rows}

    # Deduplicate systems — one ESI call per unique destination
    unique_sys = list(set(loc_to_sys.values()))

    # Jump counts are static: stargates don't move, so a route's length only ever
    # changes when the developer edits the map. Without a cache this endpoint fired one ESI
    # call per unique destination system (482 on this account) EVERY time it ran.
    # The pair is stored normalised (low, high) because the gate network is
    # undirected — the shortest path is the same in both directions.
    # Two short-lived connections rather than one held open, because the ESI
    # fan-out below sits between them: a pooled connection parked across that
    # await is one no other request can have while the network is slow.
    with _connect() as _rc:
        ensure_route_jump_table(_rc)
        cached_jumps = load_route_jumps(_rc, origin_sys, unique_sys)
    todo = [s for s in unique_sys if s not in cached_jumps and s != origin_sys]

    async def _jumps(client: httpx.AsyncClient, dest: int) -> int:
        try:
            resp = await client.get(
                f"https://esi.evetech.net/latest/route/{origin_sys}/{dest}/",
                params={"datasource": "tranquility"},
                timeout=10,
            )
            return len(resp.json()) - 1 if resp.status_code == 200 else -1
        except Exception:
            return -1

    sys_jumps = dict(cached_jumps)
    sys_jumps[origin_sys] = 0
    if todo:
        async with esi_client() as client:
            results = await asyncio.gather(*[_jumps(client, s) for s in todo])
        fresh = {s: j for s, j in zip(todo, results)}
        sys_jumps.update(fresh)
        # Only persist real answers; -1 means the lookup failed (transient) or the
        # systems aren't connected, and we must be able to retry that.
        with _connect() as _rc:
            save_route_jumps(_rc, origin_sys, {s: j for s, j in fresh.items() if j >= 0})
    conn.close()

    distances = {loc_id: sys_jumps.get(sys_id, -1) for loc_id, sys_id in loc_to_sys.items()}
    return {"ok": True, "origin_sys": origin_sys, "distances": distances,
            "from_cache": len(unique_sys) - len(todo), "fetched": len(todo)}


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------

async def _resolve_corp_container_names(
    corp_id: int,
    token: str,
    container_ids: list[int],
    corp_assets_raw: list[dict],
) -> dict[int, tuple[str, int]]:
    """Corp variant of _resolve_container_names using corp ESI endpoint."""
    asset_map = {item["item_id"]: item for item in corp_assets_raw}
    result: dict[int, tuple[str, int]] = {}

    # Same filter as the character variant, and for the same reason: posting an
    # id the corporation does not own fails the whole batch, so every container
    # falls back to its bare type name.
    owned_ids = [cid for cid in container_ids if cid in asset_map]
    if not owned_ids:
        return result

    # Same cache as the character variant. The worker fills it from the corp
    # asset sync, which already holds the role this endpoint needs.
    with _connect() as conn_names:
        custom_names = load_cached_container_names(conn_names, owned_ids)

    type_id_set = {asset_map[cid]["type_id"] for cid in container_ids if cid in asset_map}
    type_names: dict[int, str] = {}
    if type_id_set:
        with _connect() as _tc:
            rows = _tc.execute(
                text("SELECT type_id, name FROM sde_types WHERE type_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": list(type_id_set)},
            ).fetchall()
        type_names = {r[0]: r[1] for r in rows}

    for cid in container_ids:
        asset = asset_map.get(cid)
        if not asset:
            continue
        parent_loc = asset["location_id"]
        display = _container_display_name(
            custom_names.get(cid, ""), type_names.get(asset["type_id"], ""), cid
        )
        result[cid] = (display, parent_loc)

    return result


# location_flag → (label, sort order). ESI reports where inside a ship each item
# sits, which is what makes a fit readable: the flag carries the module AND any
# charge loaded into it (HiSlot0 holds both "Mega Beam Laser II" and the crystal
# in it). Ordered like the in-game fitting window so the table reads top-down.
_SLOT_LABELS: dict[str, tuple[str, int]] = {
    "DroneBay":        ("Drones", 6),
    "FighterBay":      ("Fighters", 7),
    "Cargo":           ("Cargo", 8),
    "FleetHangar":     ("Fleet hangar", 9),
    "ShipHangar":      ("Ship hangar", 10),
    "SubSystemBay":    ("Subsystem bay", 11),
    "HiddenModifiers": ("Hidden", 13),
}
for _i in range(8):
    # Wording follows the in-game fitting window, which is what people compare against.
    _SLOT_LABELS[f"HiSlot{_i}"]         = ("High power", 1)
    _SLOT_LABELS[f"MedSlot{_i}"]        = ("Medium power", 2)
    _SLOT_LABELS[f"LoSlot{_i}"]         = ("Low power", 3)
    _SLOT_LABELS[f"RigSlot{_i}"]        = ("Rig Slot", 4)
    _SLOT_LABELS[f"SubSystemSlot{_i}"]  = ("Subsystem", 5)
    _SLOT_LABELS[f"FighterTube{_i}"]    = ("Fighter tube", 7)

# Flags that describe a spot in a plain container or hangar, not a ship slot —
# these rows get no slot label, which is also how a container is told apart from
# a fitted ship (no labels anywhere → no Slot column).
_NON_SLOT_FLAGS: frozenset[str] = frozenset({
    "Hangar", "AutoFit", "Unlocked", "Locked", "Impounded",
    "Deliveries", "CorpDeliveries", "OfficeFolder", "Wardrobe",
})


def _slot_info(location_flag: str) -> tuple[str, int]:
    """Return (label, order) for an item's location_flag inside a ship."""
    flag = (location_flag or "").strip()
    if not flag or flag in _NON_SLOT_FLAGS or flag.startswith("CorpSAG"):
        return "", 0
    hit = _SLOT_LABELS.get(flag)
    if hit:
        return hit
    # Unknown flag — most likely a specialized hold (SpecializedFuelBay,
    # SpecializedOreHold, …) or one CCP added since. Split the CamelCase so it
    # reads as words instead of leaking the raw ESI token, and drop the
    # "Specialized" prefix, which says nothing to the reader.
    body = flag[len("Specialized"):] if flag.startswith("Specialized") else flag
    words: list[str] = []
    for ch in body:
        if ch.isupper() and words and words[-1]:
            words.append(ch)
        elif not words:
            words.append(ch)
        else:
            words[-1] += ch
    label = " ".join(w for w in words if w) or flag
    return label[:1].upper() + label[1:].lower(), 12


def _find_hangar_ship(hangar: dict, ship_type: int, owner_id: int | None) -> tuple:
    """Find the hangar row holding `ship_type`, whatever the bucket is keyed by.

    Deliberately matches on each row's own fields instead of rebuilding the
    bucket key: the key gained an is_copy element in v0.8.60 and both hull folds
    below, which reconstructed the old key shape, silently stopped matching. Ships
    were then never folded into their own container and their value excluded the
    hull. Matching on fields cannot break that way again.
    """
    for key, entry in hangar.items():
        if entry.get("type_id") != ship_type:
            continue
        if entry.get("is_blueprint_copy"):
            continue
        if owner_id is not None and entry.get("character_id") != owner_id:
            continue
        if (entry.get("quantity") or 0) <= 0:
            continue
        return key, entry
    return None, None


def _fold_ship_hulls(
    node: dict,
    type_map: dict[int, int],
    owner_map: dict[int, tuple[int, str]] | None = None,
) -> None:
    """Move a ship's hull row out of the hangar and into its own container row.

    A container whose item_id is itself an asset with a ship type IS that ship —
    its "contents" are the fit and cargo. Folding the hull in makes the ship one
    expandable row totalling hull + fit + cargo, which is the number that
    matters, and stops the same ship being listed twice (once as a hangar row,
    once as a container).

    `owner_map` is passed for personal assets, where rows carry a character; corp
    buckets have no owner, so pass None.
    """
    hangar = node["hangar"]
    for cid, items in node["containers"].items():
        ship_type = type_map.get(cid)
        if not ship_type:
            continue
        owner_id: int | None = None
        owner_name = ""
        if owner_map is not None:
            owner = owner_map.get(cid)
            if not owner:
                continue
            owner_id, owner_name = owner
        key, entry = _find_hangar_ship(hangar, ship_type, owner_id)
        if entry is None:
            continue
        entry["quantity"] -= 1
        unit_p = entry.get("unit_price")
        if entry["quantity"] > 0 and unit_p is not None:
            entry["total_value"] = unit_p * entry["quantity"]
        hull = {
            "type_id": ship_type,
            "name": entry["name"],
            "quantity": 1,
            "is_blueprint_copy": False,
            "unit_price": unit_p,
            "total_value": unit_p,   # hull = 1 unit
            # Sorts above every slot, so the ship table reads hull → fit → cargo.
            "slot": "Hull",
            "slot_order": 0,
            # The fold applies to any container whose own type sits in the hangar,
            # not just ships, so this row alone must not switch the Slot column on
            # for a plain box — see has_slots below.
            "is_hull": True,
        }
        if owner_map is not None:
            hull["character_id"] = owner_id
            hull["character_name"] = owner_name
        items[("_hull", cid)] = hull
        if entry["quantity"] == 0:
            del hangar[key]


def _prune_by_search(node: dict, container_labels: dict[int, str], query: str) -> None:
    """Filter one hangar/containers node by `query`, container-aware.

    Matching individual rows only threw away everything inside a container, so a
    fitted ship survived as a bare hull row with nothing left to expand. A
    container whose own label matches keeps ALL of its contents (you asked for
    that ship, you want its fit); otherwise it keeps just the rows that match and
    disappears when none do.
    """
    q = query.strip().lower()
    if not q:
        return
    node["hangar"] = {
        k: v for k, v in node["hangar"].items() if q in (v.get("name") or "").lower()
    }
    kept: dict = {}
    for cid, items in node["containers"].items():
        if q in (container_labels.get(cid) or "").lower():
            kept[cid] = items
            continue
        sub = {k: v for k, v in items.items() if q in (v.get("name") or "").lower()}
        if sub:
            kept[cid] = sub
    node["containers"] = kept


async def _resolve_container_names(
    char_id: int,
    token: str,
    container_ids: list[int],
    assets: list[dict],
) -> dict[int, tuple[str, int]]:
    """For container item_ids, return {container_id: (display_name, parent_location_id)}.

    display_name is the container's custom name from ESI assets/names,
    or the container type (Small Secure Container etc.) as a fallback.
    parent_location_id is the container's location_id in assets (station/structure).
    """
    asset_map = {item["item_id"]: item for item in assets}
    result: dict[int, tuple[str, int]] = {}

    # Ask this character only about items they actually own. In the "All
    # characters" view the caller passes every container id it found across the
    # whole account, and posting another pilot's item_ids to this endpoint made
    # the call fail for the whole batch — so nobody got a custom name and every
    # assembled ship fell back to its bare hull type. Filtering also keeps the
    # request small instead of sending the same ids once per character.
    owned_ids = [cid for cid in container_ids if cid in asset_map]
    if not owned_ids:
        return result

    # From the cache the sync worker fills. A name it has not seen simply is
    # not there, and the display falls back to the container's type below —
    # which is the same thing that happened when the POST failed, so nothing
    # about the rendering changed except that it no longer costs a round trip.
    with _connect() as conn_names:
        custom_names = load_cached_container_names(conn_names, owned_ids)

    type_id_set = {asset_map[cid]["type_id"] for cid in container_ids if cid in asset_map}
    type_names: dict[int, str] = {}
    if type_id_set:
        with _connect() as _tc:
            rows = _tc.execute(
                text("SELECT type_id, name FROM sde_types WHERE type_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": list(type_id_set)},
            ).fetchall()
        type_names = {r[0]: r[1] for r in rows}

    for cid in container_ids:
        asset = asset_map.get(cid)
        if not asset:
            continue
        parent_loc = asset["location_id"]
        display = _container_display_name(
            custom_names.get(cid, ""), type_names.get(asset["type_id"], ""), cid
        )
        result[cid] = (display, parent_loc)

    return result


@router.get("/blueprints", response_class=HTMLResponse)
async def blueprints_page(request: Request, search: str = "", view: str = ""):
    conn = get_conn()
    all_chars = all_characters()

    # Resolve selected character(s) — same toggle pattern as /assets
    selected_chars: list[tuple[int, str]] = []
    if view == "all":
        selected_chars = list(all_chars)
    elif view.isdigit():
        cid = int(view)
        match = next((c for c in all_chars if c[0] == cid), None)
        if match:
            selected_chars = [match]
    if not selected_chars:
        active = get_active_character(request, conn)
        if active:
            selected_chars = [active]
    show_char_badge = view == "all" and len(all_chars) > 1

    bp_list: list[dict] = []
    bps_by_char: dict[int, list] = {}
    primary_token: str | None = None
    char_name_by_id = {cid: name for cid, name in all_chars}

    bp_unsynced: list[str] = []
    bp_oldest: list[float] = []
    if selected_chars:
        all_unique_type_ids: set[int] = set()
        for cid_sel, _name in selected_chars:
            tok = await _valid_token_async(cid_sel)
            primary_token = primary_token or tok
            with _connect() as _bc:
                bps_for, bp_at = load_cached_blueprints(_bc, cid_sel)
            if bps_for is None:
                bp_unsynced.append(_name or str(cid_sel))
                bps_for = []
            else:
                bp_oldest.append(bp_at)
            bps_by_char[cid_sel] = bps_for
            all_unique_type_ids |= {bp.type_id for bp in bps_for}
        # None, not a client: every blueprint type is in the SDE, and a page
        # that must not fetch should not be given the means to.
        names = await resolve_names_bulk(conn, list(all_unique_type_ids), None)

        if all_unique_type_ids:
            with _connect() as _pc:
                prod_rows = _pc.execute(
                    text("SELECT blueprint_type_id, product_type_id"
                         " FROM sde_blueprint_products"
                         " WHERE blueprint_type_id IN :ids"
                         " AND activity IN ('manufacturing','reaction')")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": list(all_unique_type_ids)},
                ).fetchall()
            product_type_map = {r[0]: r[1] for r in prod_rows}
        else:
            product_type_map = {}

        for owner_id, bps in bps_by_char.items():
            owner_name = char_name_by_id.get(owner_id, "")
            for bp in bps:
                name = names.get(bp.type_id, f"Unknown ({bp.type_id})")
                if search and search.lower() not in name.lower():
                    continue
                bp_list.append({
                    "name": name,
                    "type_id": bp.type_id,
                    "product_type_id": product_type_map.get(bp.type_id, bp.type_id),
                    "is_original": bp.is_original,
                    "me": bp.material_efficiency,
                    "te": bp.time_efficiency,
                    "runs": "∞" if bp.runs == -1 else bp.runs,
                    "location_id": bp.location_id,
                    "character_id": owner_id,
                    "character_name": owner_name,
                })
        bp_list.sort(key=lambda x: x["name"])

    token = primary_token
    char = selected_chars[0] if selected_chars else None
    char_id = char[0] if char else 0

    from collections import defaultdict

    # Aggregate assets across selected chars for container detection
    assets: list[dict] = []
    assets_by_char: dict[int, list[dict]] = {}
    for cid_sel, _ in selected_chars:
        with _connect() as _ac:
            a = _load_assets_from_cache(_ac, cid_sel)
        assets_by_char[cid_sel] = a
        assets.extend(a)
    asset_item_ids = {item["item_id"] for item in assets}

    all_raw_loc_ids = list({bp["location_id"] for bp in bp_list})
    container_ids = [lid for lid in all_raw_loc_ids if lid in asset_item_ids]
    structure_ids = [lid for lid in all_raw_loc_ids if lid not in asset_item_ids]

    # Resolve station names
    loc_names = {}
    if structure_ids:
        with _connect() as _lc:
            loc_names = await resolve_station_names_bulk(structure_ids, token, _lc)

    # Resolve container names + their parent stations (per char)
    container_info: dict[int, tuple[str, int]] = {}
    if container_ids:
        for owner_id, _ in selected_chars:
            tok = await _valid_token_async(owner_id)
            if not tok:
                continue
            owner_assets = assets_by_char.get(owner_id, [])
            owner_info = await _resolve_container_names(
                owner_id, tok, container_ids, owner_assets,
            )
            for k, v in owner_info.items():
                container_info.setdefault(k, v)
        parent_ids_to_resolve = list({info[1] for info in container_info.values()
                                      if info[1] not in loc_names})
        if parent_ids_to_resolve and token:
            with _connect() as _lc:
                parent_names = await resolve_station_names_bulk(
                    parent_ids_to_resolve, token, _lc)
            loc_names.update(parent_names)

    # Build the hierarchy: {station_id: {"hangar": [...], "containers": {cid: {"name": ..., "bps": [...]}}}}
    station_data: dict[int, dict] = {}

    def _get_station(sid: int) -> dict:
        if sid not in station_data:
            station_data[sid] = {"hangar": [], "containers": {}}
        return station_data[sid]

    for bp in bp_list:
        lid = bp["location_id"]
        if lid in container_info:
            container_name, parent_loc = container_info[lid]
            st = _get_station(parent_loc)
            if lid not in st["containers"]:
                st["containers"][lid] = {"name": container_name, "bps": []}
            st["containers"][lid]["bps"].append(bp)
        else:
            _get_station(lid)["hangar"].append(bp)

    # Convert to a list sorted by total count
    def _station_total(sd: dict) -> int:
        return len(sd["hangar"]) + sum(len(c["bps"]) for c in sd["containers"].values())

    stations = sorted(
        [
            {
                "loc_id": sid,
                "name": loc_names.get(sid, str(sid)),
                "hangar": sd["hangar"],
                "containers": sorted(sd["containers"].values(), key=lambda c: c["name"]),
                "total": _station_total(sd),
            }
            for sid, sd in station_data.items()
        ],
        key=lambda s: -s["total"],
    )

    conn.close()
    return _tr("blueprints.html", request, {
        "stations": stations,
        "search": search,
        "total": len(bp_list),
        "view": view or "",
        "show_char_badge": show_char_badge,
        "cached_at": min(bp_oldest) if bp_oldest else 0.0,
        "unsynced": bp_unsynced,
    })


# ── Static map data: jump counts between systems ─────────────────────────────
# /route/{a}/{b}/ answers are static (stargates don't move), so they are cached
# permanently. Pairs are stored normalised (low, high): the gate network is
# undirected, so the shortest path is the same both ways and one row serves both.

def ensure_route_jump_table(conn: Connection) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists.

    **Guarded on the dialect, and the guard is not cosmetic.** `ensure_schema`
    memoises what it has already applied by asking `PRAGMA database_list` which
    file it is looking at, and that is a syntax error on Postgres — so calling
    it there fails rather than doing nothing. There is also nothing for it to
    do: `route_jump_cache` is in the migration history, so on any backend built
    by `upgrade_to_head` the table already exists. This shim only covers the
    SQLite database that predates that history.
    """
    if conn.engine.dialect.name != "sqlite":
        return
    ensure_db_schema(dbapi(conn))


# The variable cap is a *compile-time* setting: 999 before SQLite 3.32, 32,766 on
# the build here, 65,535 on Postgres. Every other chunked `IN` in this codebase
# binds one parameter per element, so a chunk of 900 costs 900. **This one binds
# two** — the row-value pair `(sys_a, sys_b)` — so the chunk counts destinations
# while the cap counts parameters, and the two are a factor of 2 apart. Sized in
# destinations, halved: 450 destinations is 900 parameters, under 999 everywhere.
_ROUTE_CHUNK = 450          # destinations, not parameters — see above


def load_route_jumps(conn: Connection, origin: int, dests: list[int]) -> dict[int, int]:
    """Cached jump counts from `origin` to each of `dests` ({dest: jumps}).

    The `IN` is a **row-value** comparison over the normalised pair, which is
    the one construct here that is not a mechanical rewrite: the expanding
    bindparam is handed a list of *tuples* and renders `(:a1, :b1), (:a2, :b2)`.
    Checked on both backends before this was written rather than assumed.
    """
    if not dests:
        return {}
    out: dict[int, int] = {}
    for chunk_start in range(0, len(dests), _ROUTE_CHUNK):
        chunk = dests[chunk_start:chunk_start + _ROUTE_CHUNK]
        pairs = [(min(origin, d), max(origin, d)) for d in chunk]
        rows = conn.execute(
            text("SELECT sys_a, sys_b, jumps FROM route_jump_cache"
                 " WHERE (sys_a, sys_b) IN :pairs")
            .bindparams(bindparam("pairs", expanding=True)),
            {"pairs": pairs},
        ).fetchall()
        for a, b, j in rows:
            out[b if a == origin else a] = j
    return out


def save_route_jumps(conn: Connection, origin: int, jumps: dict[int, int]) -> None:
    """Upsert the normalised pairs. Commits — see `test_the_writer_commits`.

    The `if not jumps` guard was redundant under `sqlite3`, whose `executemany`
    treats an empty sequence as a no-op. It is **load-bearing now**: SQLAlchemy
    raises `StatementError: A value is required for bind parameter` when handed
    an empty parameter list. `test_saving_nothing_writes_nothing` predicted that
    before the rewrite and is what holds it.
    """
    if not jumps:
        return
    now = _time.time()
    conn.execute(
        text("INSERT INTO route_jump_cache (sys_a, sys_b, jumps, cached_at)"
             " VALUES (:sys_a, :sys_b, :jumps, :cached_at)"
             " ON CONFLICT (sys_a, sys_b) DO UPDATE SET"
             " jumps=excluded.jumps, cached_at=excluded.cached_at"),
        [{"sys_a": min(origin, d), "sys_b": max(origin, d), "jumps": j,
          "cached_at": now} for d, j in jumps.items()],
    )
    conn.commit()
