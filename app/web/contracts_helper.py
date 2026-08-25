"""
Public contracts — per-region index into a SQLite cache + local full-text search.

Fetches ALL public contracts of the chosen region (metadata) and their items
(1 call/contract), stores them in the cache, and then anything can be searched
over it (by item, type, price) without further ESI calls. See the discussion: the
only way to search by item, because the metadata listing does not contain items and
the `title` is usually empty.
"""
from __future__ import annotations
import asyncio
import json as _json
import time

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from app.character import contracts as contracts_api
from app.esi.client import esi_client
from app.db.schema import ensure_schema as ensure_db_schema


def ensure_public_contract_tables(conn: Connection) -> None:
    """Schema shim. The tables live in app/db/schema.py; this only guarantees
    they exist, and only on SQLite — `app/db/schema.py` memoises by asking
    `PRAGMA database_list`, which is a syntax error on Postgres, where the
    schema arrives through Alembic instead."""
    if conn.engine.dialect.name != "sqlite":
        return
    ensure_db_schema(conn.connection.driver_connection)


def get_index_status(conn: Connection, region_id: int) -> dict | None:
    ensure_public_contract_tables(conn)
    row = conn.execute(
        text("SELECT indexed_at, contract_count FROM public_contract_meta"
             " WHERE region_id=:region_id"),
        {"region_id": region_id},
    ).fetchone()
    if not row:
        return None
    return {"indexed_at": row[0], "contract_count": row[1]}


def _store(conn: Connection, region_id: int, contracts: list[dict],
           items_by_cid: dict[int, list[dict]]) -> None:
    ensure_public_contract_tables(conn)
    cids = [c["contract_id"] for c in contracts if c.get("contract_id")]
    # The region's old index goes first. Without it a contract that expired
    # between indexes stays in the search results forever.
    conn.execute(text("DELETE FROM public_contracts WHERE region_id=:region_id"),
                 {"region_id": region_id})
    if cids:
        # Keyed on contract_id rather than region, because a contract can move
        # between indexes; unscoped by contract it would double every line.
        conn.execute(
            text("DELETE FROM public_contract_items WHERE contract_id IN :cids")
            .bindparams(bindparam("cids", expanding=True)),
            {"cids": cids})
    if contracts:
        conn.execute(
            text("INSERT INTO public_contracts (contract_id, region_id, type, price,"
                 " reward, collateral, buyout, volume, date_expired, title,"
                 " start_location_id, end_location_id, issuer_id)"
                 " VALUES (:contract_id, :region_id, :type, :price, :reward,"
                 " :collateral, :buyout, :volume, :date_expired, :title,"
                 " :start_location_id, :end_location_id, :issuer_id)"
                 " ON CONFLICT (contract_id) DO UPDATE SET"
                 " region_id=excluded.region_id, type=excluded.type,"
                 " price=excluded.price, reward=excluded.reward,"
                 " collateral=excluded.collateral, buyout=excluded.buyout,"
                 " volume=excluded.volume, date_expired=excluded.date_expired,"
                 " title=excluded.title,"
                 " start_location_id=excluded.start_location_id,"
                 " end_location_id=excluded.end_location_id,"
                 " issuer_id=excluded.issuer_id"),
            [{"contract_id": c.get("contract_id"), "region_id": region_id,
              "type": c.get("type"), "price": c.get("price") or 0,
              "reward": c.get("reward") or 0,
              "collateral": c.get("collateral") or 0,
              "buyout": c.get("buyout") or 0, "volume": c.get("volume") or 0,
              "date_expired": c.get("date_expired", ""),
              "title": c.get("title") or "",
              "start_location_id": c.get("start_location_id"),
              "end_location_id": c.get("end_location_id"),
              "issuer_id": c.get("issuer_id")}
             for c in contracts],
        )
    item_rows = []
    for cid, items in items_by_cid.items():
        for it in items:
            if it.get("type_id"):
                item_rows.append({"contract_id": cid, "type_id": it["type_id"],
                                  "quantity": it.get("quantity", 0),
                                  "is_included": 1 if it.get("is_included", True) else 0})
    if item_rows:
        conn.execute(
            text("INSERT INTO public_contract_items"
                 " (contract_id, type_id, quantity, is_included)"
                 " VALUES (:contract_id, :type_id, :quantity, :is_included)"),
            item_rows)
    conn.execute(
        text("INSERT INTO public_contract_meta (region_id, indexed_at, contract_count)"
             " VALUES (:region_id, :indexed_at, :contract_count)"
             " ON CONFLICT (region_id) DO UPDATE SET"
             " indexed_at=excluded.indexed_at,"
             " contract_count=excluded.contract_count"),
        {"region_id": region_id, "indexed_at": time.time(),
         "contract_count": len(contracts)})
    # Commits, unlike every other writer in this codebase. The SSE generator
    # that drives it streams progress across a whole region and must not hold a
    # write open for the duration, so this owns its own boundary.
    conn.commit()


async def stream_public_index(conn: Connection, region_id: int):
    """SSE generator: fetch the listing (pages) + items (per contract) and store them."""
    ensure_public_contract_tables(conn)
    total_pages = [0]
    done_pages = [0]
    holder: dict = {}

    def _list_prog(done, total):
        done_pages[0] = done
        total_pages[0] = total

    async def _run_list():
        async with esi_client() as client:
            holder["list"] = await contracts_api.fetch_public_contracts(
                client, region_id, progress_cb=_list_prog)

    task = asyncio.create_task(_run_list())
    while not task.done():
        tp = total_pages[0]
        pct = int(done_pages[0] * 40 / tp) if tp else 0
        yield f"data: {_json.dumps({'phase':'list','done':done_pages[0],'total':tp,'pct':pct})}\n\n"
        await asyncio.sleep(0.4)
    await task
    contracts = holder.get("list", [])

    # Items only for types with contents (courier/loan usually have no items).
    item_contracts = [c for c in contracts if c.get("type") in ("item_exchange", "auction")]
    total_items = len(item_contracts)
    done_items = [0]
    items_by_cid: dict[int, list[dict]] = {}
    lock = asyncio.Lock()

    async def _one(client, c):
        its = await contracts_api.fetch_public_contract_items(client, c["contract_id"])
        async with lock:
            if its:
                items_by_cid[c["contract_id"]] = its
            done_items[0] += 1

    async def _run_items():
        async with esi_client() as client:
            await asyncio.gather(*[_one(client, c) for c in item_contracts],
                                 return_exceptions=True)

    yield f"data: {_json.dumps({'phase':'items','done':0,'total':total_items,'pct':40})}\n\n"
    task2 = asyncio.create_task(_run_items())
    while not task2.done():
        pct = 40 + (int(done_items[0] * 55 / total_items) if total_items else 55)
        yield f"data: {_json.dumps({'phase':'items','done':done_items[0],'total':total_items,'pct':pct})}\n\n"
        await asyncio.sleep(0.4)
    await task2

    _store(conn, region_id, contracts, items_by_cid)
    yield f"data: {_json.dumps({'done':True,'pct':100,'contract_count':len(contracts)})}\n\n"


def search_public_contracts(conn: Connection, region_id: int, *,
                            item: str = "", ctype: str = "", max_price: float | None = None,
                            limit: int = 300) -> list[dict]:
    """Local search over the indexed region.

    The clauses are still assembled, because which ones apply depends on what
    the user filled in — but the values now go into a **dict keyed by name**
    rather than a positional list built alongside the string. That is the point
    of the conversion here: the previous version appended each parameter as its
    clause fired, so the binding order was a property of the branch order, and
    a mispairing would have returned plausible rows rather than raising.
    """
    ensure_public_contract_tables(conn)
    where = ["c.region_id = :region_id"]
    params: dict = {"region_id": region_id, "limit": limit}
    joins = ""
    if item.strip():
        joins = (" JOIN public_contract_items i ON i.contract_id = c.contract_id"
                 " JOIN sde_types t ON t.type_id = i.type_id")
        where.append("t.name LIKE :item")
        params["item"] = f"%{item.strip()}%"
    if ctype:
        where.append("c.type = :ctype")
        params["ctype"] = ctype
    if max_price is not None:
        # `is not None`, not truthiness: zero is a real ceiling — it selects the
        # free contracts — and treating it as "no filter" returns the region.
        where.append("c.price <= :max_price")
        params["max_price"] = max_price
    sql = (f"SELECT DISTINCT c.contract_id, c.type, c.price, c.reward, c.collateral, "
           f"c.volume, c.date_expired, c.title, c.start_location_id, c.end_location_id, "
           f"c.issuer_id FROM public_contracts c{joins} WHERE {' AND '.join(where)} "
           f"ORDER BY c.price LIMIT :limit")
    cols = ["contract_id", "type", "price", "reward", "collateral", "volume",
            "date_expired", "title", "start_location_id", "end_location_id", "issuer_id"]
    return [dict(zip(cols, row))
            for row in conn.execute(text(sql), params).fetchall()]


def best_contract_price(conn: Connection, region_id: int, type_id: int) -> dict | None:
    """Cheapest price/unit of a product from public item_exchange contracts in the region.
    Prefers single-item contracts (clean price/unit); if there is none, it takes a
    bundle (multiple items) and marks is_bundle=True (the price/unit is then only
    indicative — it also covers the other items in the bundle). Returns None if the product is nowhere."""
    ensure_public_contract_tables(conn)
    rows = conn.execute(text("""
        SELECT c.contract_id, c.price, pi.quantity,
               (SELECT COUNT(*) FROM public_contract_items x
                 WHERE x.contract_id = c.contract_id AND x.is_included = 1) AS incl
        FROM public_contracts c
        JOIN public_contract_items pi ON pi.contract_id = c.contract_id
        WHERE c.region_id = :region_id AND c.type = 'item_exchange' AND c.price > 0
          AND pi.type_id = :type_id AND pi.is_included = 1
    """), {"region_id": region_id, "type_id": type_id}).fetchall()
    singles: list[tuple[float, int]] = []
    bundles: list[tuple[float, int]] = []
    for cid, price, qty, incl in rows:
        if not qty or qty <= 0:
            continue
        per_unit = price / qty
        (singles if incl == 1 else bundles).append((per_unit, cid))
    if singles:
        per_unit, cid = min(singles)
        return {"price": per_unit, "is_bundle": False, "contract_id": cid,
                "single_count": len(singles), "bundle_count": len(bundles)}
    if bundles:
        per_unit, cid = min(bundles)
        return {"price": per_unit, "is_bundle": True, "contract_id": cid,
                "single_count": 0, "bundle_count": len(bundles)}
    return None


def get_contract_items(conn: Connection, contract_id: int) -> list[dict]:
    ensure_public_contract_tables(conn)
    # `'#'||i.type_id` is `text || integer`, which both backends accept — the
    # LEFT JOIN is what matters, since the SDE subset does not carry every type
    # and an inner join would drop those lines rather than name them by id.
    rows = conn.execute(
        text("SELECT i.type_id, i.quantity, i.is_included,"
             " COALESCE(t.name, '#'||i.type_id)"
             " FROM public_contract_items i"
             " LEFT JOIN sde_types t ON t.type_id = i.type_id"
             " WHERE i.contract_id=:contract_id"),
        {"contract_id": contract_id}).fetchall()
    return [{"type_id": r[0], "quantity": r[1], "included": bool(r[2]), "name": r[3]}
            for r in rows]
