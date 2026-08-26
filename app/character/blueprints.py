"""
Fetching a character's blueprints from ESI.
  quantity == -1 → original (BPO), unlimited runs
  quantity == -2 → copy     (BPC), remaining runs in the 'runs' field
"""
from __future__ import annotations
from dataclasses import dataclass
import time
import json
import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection
from app.db.schema import ensure_schema as ensure_db_schema

ESI_BASE = "https://esi.evetech.net/latest"
CACHE_TTL = 60 * 15  # 15 minutes


@dataclass
class CharBlueprint:
    item_id: int
    type_id: int         # type_id of the blueprint (not the product!)
    location_id: int
    location_flag: str
    is_original: bool    # True = BPO, False = BPC
    runs: int            # -1 = unlimited (BPO), otherwise remaining runs
    material_efficiency: int   # ME 0-10
    time_efficiency: int       # TE 0-20


def _load_cache(conn: Connection, character_id: int) -> list[dict] | None:
    """The fetcher's reader, which enforces `CACHE_TTL`.

    `load_cached_blueprints` below reads the same row and ignores the TTL. The
    two are not redundant: the TTL answers "is another round trip worth it",
    which is this caller's question and not a page's.
    """
    row = conn.execute(
        text("SELECT data_json, cached_at FROM char_blueprints_cache"
             " WHERE character_id=:cid"),
        {"cid": character_id},
    ).fetchone()
    if row and (time.time() - (row[1] or 0)) < CACHE_TTL:
        return json.loads(row[0])
    return None


def _save_cache(conn: Connection, character_id: int, data: list[dict]):
    # DELETE then INSERT rather than an upsert: char_blueprints_cache has no
    # primary key and no UNIQUE(character_id) to conflict against. One row per
    # character is the invariant, and without the delete a second save leaves
    # two — after which which one a later `fetchone()` wins is a question about
    # row order, and SQLite and Postgres need not answer it the same way.
    conn.execute(text("DELETE FROM char_blueprints_cache WHERE character_id=:cid"),
                 {"cid": character_id})
    conn.execute(
        text("INSERT INTO char_blueprints_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :cached_at)"),
        {"cid": character_id, "data": json.dumps(data), "cached_at": time.time()},
    )
    conn.commit()


def load_cached_blueprints(conn: Connection,
                           character_id: int) -> tuple[list[CharBlueprint] | None, float]:
    """(blueprints, cached_at) at any age, or (None, 0) if never synced.

    Ignores `CACHE_TTL` for the same reason as the assets reader beside it: the
    TTL answers "is another round trip worth it", which is a question for the
    fetcher and not for a page that must not make round trips.
    """
    row = conn.execute(
        text("SELECT data_json, cached_at FROM char_blueprints_cache"
             " WHERE character_id=:cid"),
        {"cid": character_id},
    ).fetchone()
    if not row:
        return None, 0.0
    try:
        return _parse_blueprints(json.loads(row[0])), float(row[1] or 0.0)
    except (ValueError, TypeError, KeyError, AttributeError):
        # None, not [] — "never synced" and "owns none" are different answers
        # and the pages act differently on them.
        #
        # `AttributeError` was missing until v0.9.76, and a payload that parsed
        # but was not a list of dicts therefore escaped as a **500** on
        # `/assets`, `/blueprints` and `/plan` rather than reading as
        # never-synced like every other corrupt payload.
        #
        # What made it *this* reader and not the assets one beside it is a
        # single line's ordering, which is worth knowing because it is the kind
        # of thing an innocuous refactor flips. `_parse_assets` opens with
        # `item["item_id"]`, so a non-dict raises `TypeError` — caught.
        # `_parse_blueprints` opens with `item.get("quantity", -1)`, so the same
        # payload raises `AttributeError` — not caught. The two functions are
        # otherwise identical. Catching both means the guarantee no longer
        # depends on which access happens to come first.
        return None, 0.0


async def fetch_blueprints(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: Connection,
    force_refresh: bool = False,
) -> list[CharBlueprint]:
    """Fetch all of a character's blueprints (paginated), with caching."""
    if not force_refresh:
        cached = _load_cache(conn, character_id)
        if cached is not None:
            return _parse_blueprints(cached)

    headers = {"Authorization": f"Bearer {access_token}"}
    all_items: list[dict] = []
    page = 1

    while True:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/blueprints/",
            params={"datasource": "tranquility", "page": page},
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        items = r.json()
        all_items.extend(items)

        total_pages = int(r.headers.get("x-pages", 1))
        if page >= total_pages:
            break
        page += 1

    _save_cache(conn, character_id, all_items)
    return _parse_blueprints(all_items)


def _parse_blueprints(raw: list[dict]) -> list[CharBlueprint]:
    result = []
    for item in raw:
        qty = item.get("quantity", -1)
        result.append(CharBlueprint(
            item_id            = item["item_id"],
            type_id            = item["type_id"],
            location_id        = item["location_id"],
            location_flag      = item.get("location_flag", "Hangar"),
            is_original        = (qty == -1),
            runs               = item.get("runs", -1),
            material_efficiency = item.get("material_efficiency", 0),
            time_efficiency    = item.get("time_efficiency", 0),
        ))
    return result
