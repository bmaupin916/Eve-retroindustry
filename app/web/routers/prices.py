"""Market prices: the /prices page, its refresh streams, and the price APIs.

Moved out of `main.py` unchanged (W6). The three SSE endpoints are the reason
this router went first: `StreamingResponse` from an `APIRouter` is the one
thing in the split that behaves differently from a plain `@app.get`, and it is
better to find that out with twelve routes moved than with sixty.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.auth.token_store import list_characters
from app.esi.client import esi_client
from app.market.prices import (
    JITA_REGION,
    TRADE_HUBS,
    ensure_price_table,
    fetch_station_volumes,
    fetch_structure_market,
    get_cached_station_volumes,
    get_station_volumes_any_age,
)
from app.web.deps import (
    all_characters,
    any_character,
    character_row,
    _ensure_groups_populated,
    _load_assets_from_cache,
    _load_blueprints_from_cache,
    _tr,
    get_active_token,
    get_conn,
)
from app.db.conn import connect as _connect
from app.web.location_resolver import (
    get_region_for_location,
    load_location_names_from_db,
    resolve_station_names_bulk,
)
from app.web.prices_helper import (
    get_all_hub_prices,
    get_all_price_items,
    get_hub_cache_stats,
    get_price_cache_stats,
    get_price_history,
    refresh_jita_prices_all,
    set_custom_price,
    stream_hub_refresh,
    stream_jita_refresh,
)

router = APIRouter()

async def _bg_fetch_prices(type_ids: list[int]) -> None:
    """Fire-and-forget: fetch Jita prices for the given type_ids using a fresh connection."""
    from app.market.prices import fetch_jita_prices_bulk as _bulk
    conn = get_conn()
    try:
        async with esi_client() as client:
            await _bulk(client, conn, type_ids, force=True)
    except Exception:
        pass
    finally:
        conn.close()


def _refresh_type_ids(conn) -> list[int]:
    """Full set of type_ids to refresh — everything tradeable in EVE (market_group_id IS NOT NULL)
    plus user assets/blueprints/materials and currently cached types.

    The tradeable filter covers modules, ammo, ships, skillbooks, structures, etc. — everything
    that can be bought/sold on the market.
    """
    # Aggregate type IDs across ALL characters
    asset_type_ids: set[int] = set()
    bp_type_ids: set[int] = set()
    for char_id, _name in all_characters():
        asset_type_ids |= {a["type_id"] for a in _load_assets_from_cache(conn, char_id)}
        bp_type_ids |= {bp["type_id"] for bp in _load_blueprints_from_cache(conn, char_id)}
    mat_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT material_type_id FROM sde_blueprint_materials"
    ).fetchall()}
    cached_ids = {r[0] for r in conn.execute(
        "SELECT type_id FROM market_price_cache"
    ).fetchall()}
    # All published tradeable types (modules, ammo, ships, skillbooks, ...)
    tradeable_ids = {r[0] for r in conn.execute(
        "SELECT type_id FROM sde_types WHERE published=1 AND market_group_id IS NOT NULL"
    ).fetchall()}
    return list(asset_type_ids | bp_type_ids | mat_ids | cached_ids | tradeable_ids)


@router.post("/prices/refresh", response_class=HTMLResponse)
async def prices_refresh(request: Request):
    conn = get_conn()

    all_ids = _refresh_type_ids(conn)

    refreshed = await refresh_jita_prices_all(conn, all_ids)
    stats = get_price_cache_stats(conn)
    items = get_all_price_items(conn)
    conn.close()

    return _tr("prices.html", request, {
        "stats": stats,
        "refreshed_count": refreshed,
        "total_requested": len(all_ids),
        "items": items,
    })


@router.get("/prices/refresh/stream")
async def prices_refresh_stream():
    conn = get_conn()
    all_ids = _refresh_type_ids(conn)

    async def event_gen():
        try:
            async for chunk in stream_jita_refresh(conn, all_ids):
                yield chunk
        except Exception:
            pass
        finally:
            conn.close()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/prices/refresh/hub/{region_id}/stream")
async def prices_refresh_hub_stream(region_id: int):
    """SSE refresh for one secondary trade hub (Amarr/Dodixie/Rens/Hek). Same
    pipeline as the Jita refresh, writing to hub_price_cache."""
    from fastapi.responses import JSONResponse
    if region_id not in TRADE_HUBS:
        return JSONResponse({"error": "unknown hub"}, status_code=404)
    conn = get_conn()
    all_ids = _refresh_type_ids(conn)

    async def event_gen():
        try:
            async for chunk in stream_hub_refresh(conn, all_ids, region_id):
                yield chunk
        except Exception:
            pass
        finally:
            conn.close()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/prices/history")
async def api_price_history(type_id: int, region_id: int = JITA_REGION):
    """Daily market history (~1 year) for a type — powers the price-history chart
    opened from the Prices table. Defaults to Jita / The Forge."""
    conn = get_conn()
    try:
        series = await get_price_history(conn, region_id, type_id)
    finally:
        conn.close()
    return {"type_id": type_id, "region_id": region_id, "series": series}


@router.get("/api/prices/orders")
async def api_market_orders(request: Request, type_id: int, region_id: int = JITA_REGION,
                            location_id: int = 0):
    """Live market orders for one type — the "Market" tab of the item popup.

    Without location_id → region-wide orders (Jita / hub = the in-game regional
    market). With location_id → only THAT station/citadel's orders: a private
    citadel's orders are not in the public region feed, so we read its authed
    structure market directly (otherwise the tab showed unrelated region orders
    and never matched the table's per-station sell / available)."""
    conn = get_conn()
    token = get_active_token(request, conn)
    try:
        orders: list[dict] = []
        is_structure = bool(location_id) and location_id >= 1_000_000_000_000

        if is_structure:
            if not token:
                return {"ok": False, "error": "Sign-in is required to read this citadel's market."}
            async with esi_client() as client:
                page = 1
                while page <= 30:
                    try:
                        r = await client.get(
                            f"https://esi.evetech.net/latest/markets/structures/{location_id}/",
                            params={"datasource": "tranquility", "page": page},
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=20,
                        )
                    except Exception:
                        break
                    if r.status_code == 403:
                        return {"ok": False, "error": "No access to this structure's market."}
                    if r.status_code != 200:
                        break
                    batch = r.json()
                    if not isinstance(batch, list) or not batch:
                        break
                    orders.extend(o for o in batch if o.get("type_id") == type_id)
                    if page >= int(r.headers.get("X-Pages", 1)):
                        break
                    page += 1
        else:
            async with esi_client() as client:
                page = 1
                while page <= 20:
                    try:
                        r = await client.get(
                            f"https://esi.evetech.net/latest/markets/{region_id}/orders/",
                            params={"type_id": type_id, "order_type": "all",
                                    "datasource": "tranquility", "page": page},
                            timeout=20,
                        )
                    except Exception:
                        break
                    if r.status_code != 200:
                        break
                    batch = r.json()
                    if not isinstance(batch, list) or not batch:
                        break
                    orders.extend(batch)
                    if page >= int(r.headers.get("X-Pages", 1)):
                        break
                    page += 1
            if location_id:   # custom NPC station → keep only that station's orders
                orders = [o for o in orders if o.get("location_id") == location_id]

        sells = sorted((o for o in orders if not o.get("is_buy_order")),
                       key=lambda o: o.get("price", 0))
        buys = sorted((o for o in orders if o.get("is_buy_order")),
                      key=lambda o: -o.get("price", 0))

        loc_ids = list({o.get("location_id") for o in orders if o.get("location_id")})
        loc_names: dict[int, str] = {}
        if loc_ids:
            with _connect() as _lc:
                try:
                    loc_names = await resolve_station_names_bulk(
                        loc_ids, token=token, conn=_lc)
                except Exception:
                    loc_names = load_location_names_from_db(_lc)

        def _loc(lid):
            if not lid:
                return ""
            return loc_names.get(lid) or (f"Citadel #{lid}" if lid >= 1_000_000_000_000 else str(lid))

        def _pack(o):
            return {
                "price": o.get("price", 0.0),
                "qty": o.get("volume_remain", 0),
                "location": _loc(o.get("location_id")),
                "range": o.get("range"),
            }

        # Cap to the best 150 per side — enough depth, keeps the payload small
        # for super-liquid items (which can have thousands of orders).
        return {
            "ok": True, "type_id": type_id, "region_id": region_id,
            "sell": [_pack(o) for o in sells[:150]], "sell_count": len(sells),
            "buy": [_pack(o) for o in buys[:150]], "buy_count": len(buys),
        }
    finally:
        conn.close()


@router.get("/api/prices/suggest")
async def prices_suggest(q: str = ""):
    """Typeahead for the Prices search box: matching market groups + item names.
    Groups let the user discover the correct group name — e.g. typing
    "battlecruiser" surfaces "Combat Battlecruiser" / "Attack Battlecruiser"
    (there is no group literally named "Battlecruiser"), which the exact-match
    group search then resolves."""
    ql = q.strip().lower()
    if len(ql) < 2:
        return {"groups": [], "items": []}
    conn = get_conn()
    try:
        await _ensure_groups_populated(conn)
        like = f"%{ql}%"
        groups = [
            {"name": r[0], "count": r[1]}
            for r in conn.execute(
                """SELECT g.name, COUNT(t.type_id) AS n
                   FROM sde_groups g
                   JOIN sde_types t ON t.group_id = g.group_id AND t.published = 1
                   WHERE LOWER(g.name) LIKE ?
                   GROUP BY g.group_id
                   HAVING n > 0
                   ORDER BY (LOWER(g.name) = ?) DESC, n DESC, g.name
                   LIMIT 8""",
                (like, ql),
            ).fetchall()
        ]
        items = [
            {"type_id": r[0], "name": r[1]}
            for r in conn.execute(
                """SELECT type_id, name FROM sde_types
                   WHERE published = 1 AND LOWER(name) LIKE ?
                   ORDER BY (LOWER(name) LIKE ?) DESC, LENGTH(name), name
                   LIMIT 10""",
                (like, ql + "%"),
            ).fetchall()
        ]
        return {"groups": groups, "items": items}
    finally:
        conn.close()


@router.get("/api/prices/search")
async def prices_search(q: str = ""):
    if len(q.strip()) < 2:
        return {"mode": "name", "group_name": None, "items": []}
    conn = get_conn()
    await _ensure_groups_populated(conn)
    pattern = f"%{q.strip().lower()}%"
    import time as _time
    from app.market.prices import PRICE_CACHE_TTL
    now = _time.time()

    # Priority: group mode only on an EXACT match with the group name (e.g. "battleship"
    # → group "Battleship"). For any substring ("amarr") we prefer name search,
    # because the user is looking for a specific type by name, not all items from one of the N
    # groups containing the substring.
    group_rows = conn.execute(
        "SELECT group_id, name FROM sde_groups WHERE LOWER(name) = ? ORDER BY name",
        (q.strip().lower(),),
    ).fetchall()

    if group_rows:
        group_ids = [r[0] for r in group_rows]
        ph = ",".join("?" * len(group_ids))
        rows = conn.execute(f"""
            SELECT t.type_id, t.name, g.name AS group_name,
                   m.sell_price, m.buy_price, m.cached_at,
                   m.volume, m.jita_available
            FROM sde_types t
            JOIN sde_groups g ON g.group_id = t.group_id
            LEFT JOIN market_price_cache m ON m.type_id = t.type_id
            WHERE t.published = 1 AND t.group_id IN ({ph})
            ORDER BY g.name, t.name
            LIMIT 500
        """, group_ids).fetchall()
        found_groups = list(dict.fromkeys(r[1] for r in group_rows))
        label = ", ".join(found_groups[:3])

        # Ensure all returned types are tracked; background-fetch ones with no price yet.
        uncached = [r[0] for r in rows if r[5] is None]  # cached_at IS NULL → never fetched
        if uncached:
            conn.executemany(
                "INSERT INTO market_price_cache (type_id, sell_price, buy_price, cached_at) VALUES (?,NULL,NULL,0) ON CONFLICT (type_id) DO NOTHING",
                [(tid,) for tid in uncached],
            )
            conn.commit()
            import asyncio as _asyncio
            _asyncio.create_task(_bg_fetch_prices(uncached))

        hub_prices = get_all_hub_prices(conn, [r[0] for r in rows])
        conn.close()
        return {
            "mode": "group",
            "group_name": label,
            "fetching_prices": len(uncached) > 0,
            "items": [
                {
                    "type_id":       r[0],
                    "name":          r[1],
                    "group_name":    r[2],
                    "sell_price":    r[3],
                    "buy_price":     r[4],
                    "fresh":         bool(r[5] and (now - r[5]) < PRICE_CACHE_TTL),
                    "volume":        r[6],
                    "jita_available": r[7],
                    "hubs":          hub_prices.get(r[0], {}),
                }
                for r in rows
            ],
        }

    # Fallback: search within item names. LEFT JOIN so types without a price in
    # the cache are shown too (we're fetching them in the background). Restrict to
    # tradeable only (market_group_id) so BPCs/unpublished/off-market items aren't returned.
    rows = conn.execute("""
        SELECT t.type_id, t.name, g.name AS group_name,
               m.sell_price, m.buy_price, m.cached_at,
               m.volume, m.jita_available
        FROM sde_types t
        LEFT JOIN sde_groups g ON g.group_id = t.group_id
        LEFT JOIN market_price_cache m ON m.type_id = t.type_id
        WHERE t.published = 1
          AND t.market_group_id IS NOT NULL
          AND LOWER(t.name) LIKE ?
        ORDER BY t.name
        LIMIT 100
    """, (pattern,)).fetchall()

    # Bg-fetch for types without a price
    uncached = [r[0] for r in rows if r[5] is None]
    if uncached:
        conn.executemany(
            "INSERT INTO market_price_cache (type_id, sell_price, buy_price, cached_at) VALUES (?,NULL,NULL,0) ON CONFLICT (type_id) DO NOTHING",
            [(tid,) for tid in uncached],
        )
        conn.commit()
        import asyncio as _asyncio
        _asyncio.create_task(_bg_fetch_prices(uncached))
    hub_prices = get_all_hub_prices(conn, [r[0] for r in rows])
    conn.close()
    return {
        "mode": "name",
        "group_name": None,
        "items": [
            {
                "type_id":       r[0],
                "name":          r[1],
                "group_name":    r[2],
                "sell_price":    r[3],
                "buy_price":     r[4],
                "fresh":         bool(r[5] and (now - r[5]) < PRICE_CACHE_TTL),
                "volume":        r[6],
                "jita_available": r[7],
                "hubs":          hub_prices.get(r[0], {}),
            }
            for r in rows
        ],
    }


@router.post("/api/prices/custom")
async def api_set_custom_price(request: Request):
    body = await request.json()
    type_id = int(body["type_id"])
    price_raw = body.get("price")
    price = float(price_raw) if price_raw not in (None, "", "null") else None
    conn = get_conn()
    set_custom_price(conn, type_id, price)
    conn.close()
    return {"ok": True, "type_id": type_id, "price": price}


@router.get("/api/prices/station-volume/cached")
async def api_station_volume_cached(location_id: int):
    """Cache-only station volumes (any age) + newest cached_at — used to restore a
    previously loaded custom station on page load without a fresh ESI fetch."""
    conn = get_conn()
    try:
        res = get_station_volumes_any_age(conn, location_id)
    finally:
        conn.close()
    if not res:
        return {"ok": False}
    data, cached_at = res
    return {"ok": True, "cached_at": cached_at, "data": {
        str(k): {"volume": v[0], "best_sell": v[1], "traded_volume": v[2]}
        for k, v in data.items()
    }}


@router.get("/prices/refresh/station/stream")
async def prices_station_stream(request: Request, location_id: int):
    """Streamed custom-station load with real progress. The volume phase fetches
    7-day region history for every cached type (thousands), which is slow — the
    old fixed 90% fake bar looked frozen. Streams orders/volume progress; on done
    the client reads the now-cached data via /api/prices/station-volume/cached."""
    import json as _json
    conn = get_conn()
    token = get_active_token(request, conn)
    ensure_price_table(conn)
    try:
        with _connect() as _lc:
            region_id = await get_region_for_location(_lc, location_id, token)
    except Exception:
        region_id = None

    async def gen():
        task = None
        try:
            # Explicit "Load" always fetches fresh — never serve the 30-min cache
            # here (a previous partial/failed fetch could otherwise be replayed,
            # e.g. blank sell/available with only 7d volume). The cache still backs
            # the silent restore-on-page-load path (/station-volume/cached).
            type_ids = [r[0] for r in conn.execute("SELECT type_id FROM market_price_cache").fetchall()]
            if not type_ids:
                type_ids = _refresh_type_ids(conn)
            total = len(type_ids) or 1
            holder = [0]
            def _prog(done, _tot):
                holder[0] = done
            yield f"data: {_json.dumps({'phase': 'orders', 'pct': 4})}\n\n"

            if location_id >= 1_000_000_000:
                if not token:
                    yield f"data: {_json.dumps({'error': 'Sign-in is required to access the structure market.'})}\n\n"
                    return
                task = asyncio.create_task(fetch_structure_market(
                    conn, location_id, token, set(type_ids), region_id, progress_cb=_prog))
            else:
                task = asyncio.create_task(fetch_station_volumes(
                    conn, location_id, region_id, type_ids, progress_cb=_prog))

            while not task.done():
                await asyncio.sleep(0.5)
                pct = min(98, 8 + int(holder[0] * 90 / total))
                yield f"data: {_json.dumps({'phase': 'volumes', 'vol_done': holder[0], 'vol_total': total, 'pct': pct})}\n\n"

            exc = task.exception()
            if exc is not None:
                yield f"data: {_json.dumps({'error': str(exc) or 'Fetch failed.'})}\n\n"
                return
            task = None   # completed cleanly — don't cancel in finally
            yield f"data: {_json.dumps({'done': True, 'cached': False, 'region_id': region_id, 'pct': 100})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"
        finally:
            # If the client disconnected mid-load, cancel the background fetch so it
            # doesn't keep writing to the connection we're about to close.
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            conn.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/prices/station-volume")
async def api_station_volume(request: Request):
    body = await request.json()
    location_id = int(body["location_id"])

    conn = get_conn()
    token = get_active_token(request, conn)
    ensure_price_table(conn)
    # Region of this location — returned so the price-history chart can offer
    # "custom station" (history is region-wide; ESI has no per-structure history).
    try:
        with _connect() as _lc:
            region_id = await get_region_for_location(_lc, location_id, token)
    except Exception:
        region_id = None

    # Try the cache
    cached = get_cached_station_volumes(conn, location_id)
    if cached is not None:
        conn.close()
        return {"ok": True, "cached": True, "region_id": region_id, "data": {
            str(k): {"volume": v[0], "best_sell": v[1], "traded_volume": v[2]}
            for k, v in cached.items()
        }}

    type_ids = [r[0] for r in conn.execute("SELECT type_id FROM market_price_cache").fetchall()]
    if not type_ids:
        type_ids = _refresh_type_ids(conn)

    def _fmt(result):
        return {"ok": True, "cached": False, "region_id": region_id, "data": {
            str(k): {"volume": v[0], "best_sell": v[1], "traded_volume": v[2]}
            for k, v in result.items()
        }}

    # Player structure (Upwell citadel, Fortizar, …) — use the structure market endpoint
    if location_id >= 1_000_000_000:
        if not token:
            conn.close()
            return {"ok": False, "error": "Sign-in is required to access the structure market."}
        try:
            result = await fetch_structure_market(conn, location_id, token, set(type_ids), region_id)
        except PermissionError as e:
            conn.close()
            return {"ok": False, "error": str(e)}
        conn.close()
        return _fmt(result)

    # NPC station — regional public endpoint
    if not region_id:
        conn.close()
        return {"ok": False, "error": "Could not determine the region for this location."}

    result = await fetch_station_volumes(conn, location_id, region_id, type_ids)
    conn.close()
    return _fmt(result)


@router.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request):
    conn = get_conn()
    stats = get_price_cache_stats(conn)
    # By default render only the relevant subset (user assets + BPs + custom prices).
    # The full cache has ~19k items → rendering the whole table = 48 MB HTML. The rest
    # is loaded on demand via /api/prices/search.
    # Aggregate user type-IDs across ALL characters so prices page reflects every alt.
    relevant: set[int] = set()
    for char_id, _name in all_characters():
        relevant |= {a["type_id"] for a in _load_assets_from_cache(conn, char_id)}
        relevant |= {bp["type_id"] for bp in _load_blueprints_from_cache(conn, char_id)}
    if relevant:
        ph = ",".join("?" * len(relevant))
        bp_products = conn.execute(
            f"SELECT product_type_id FROM sde_blueprint_products"
            f" WHERE blueprint_type_id IN ({ph})",
            tuple(relevant),
        ).fetchall()
        relevant |= {r[0] for r in bp_products}
    items = get_all_price_items(conn, relevant_ids=relevant)

    # Secondary trade hubs: metadata for all, price data only for those already
    # fetched. Attach each hub's sell/buy/volume to the item rows for comparison.
    hubs = [{"region_id": rid, "name": info["name"], **get_hub_cache_stats(conn, rid)}
            for rid, info in TRADE_HUBS.items()]
    downloaded_hubs = [h for h in hubs if h["has_data"]]
    if downloaded_hubs:
        hub_prices = get_all_hub_prices(conn, [i["type_id"] for i in items])
        for it in items:
            it["hubs"] = hub_prices.get(it["type_id"], {})
    conn.close()
    return _tr("prices.html", request, {
        "stats": stats,
        "refreshed_count": None,
        "total_requested": None,
        "items": items,
        "hubs": hubs,
        "downloaded_hubs": downloaded_hubs,
    })
