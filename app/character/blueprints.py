"""
Fetching a character's blueprints from ESI.
  quantity == -1 → original (BPO), unlimited runs
  quantity == -2 → copy     (BPC), remaining runs in the 'runs' field
"""
from __future__ import annotations
from dataclasses import dataclass
import time
import sqlite3
import json
import httpx
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


def ensure_bp_table(conn: sqlite3.Connection) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists."""
    ensure_db_schema(conn)


def _load_cache(conn: sqlite3.Connection, character_id: int) -> list[dict] | None:
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_blueprints_cache WHERE character_id=?",
        (character_id,)
    ).fetchone()
    if row and (time.time() - (row[1] or 0)) < CACHE_TTL:
        return json.loads(row[0])
    return None


def _save_cache(conn: sqlite3.Connection, character_id: int, data: list[dict]):
    conn.execute("DELETE FROM char_blueprints_cache WHERE character_id=?", (character_id,))
    conn.execute(
        "INSERT INTO char_blueprints_cache (character_id, data_json, cached_at) VALUES (?,?,?)",
        (character_id, json.dumps(data), time.time())
    )
    conn.commit()


def load_cached_blueprints(conn: sqlite3.Connection,
                           character_id: int) -> tuple[list[CharBlueprint] | None, float]:
    """(blueprints, cached_at) at any age, or (None, 0) if never synced.

    Ignores `CACHE_TTL` for the same reason as the assets reader beside it: the
    TTL answers "is another round trip worth it", which is a question for the
    fetcher and not for a page that must not make round trips.
    """
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_blueprints_cache WHERE character_id=?",
        (character_id,)).fetchone()
    if not row:
        return None, 0.0
    try:
        return _parse_blueprints(json.loads(row[0])), float(row[1] or 0.0)
    except (ValueError, TypeError, KeyError):
        return None, 0.0


async def fetch_blueprints(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
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
