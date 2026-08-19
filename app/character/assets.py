"""
Loading character assets from ESI (paginated).
Returns materials available at a given station/structure.
"""
from __future__ import annotations
from dataclasses import dataclass
import time
import sqlite3
import json
import httpx
from app.db.schema import ensure_schema as ensure_db_schema

ESI_BASE  = "https://esi.evetech.net/latest"
CACHE_TTL = 60 * 10  # 10 minutes (assets change)


@dataclass
class CharAsset:
    item_id:            int
    type_id:            int
    location_id:        int
    location_flag:      str
    quantity:           int
    is_singleton:       bool   # True = unique item (ship, fitted module…)
    is_blueprint_copy:  bool   # True = BPC (blueprint copy with no market price)


def ensure_assets_table(conn: sqlite3.Connection) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists."""
    ensure_db_schema(conn)


def _load_cache(conn: sqlite3.Connection, character_id: int) -> list[dict] | None:
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_assets_cache WHERE character_id=?",
        (character_id,)
    ).fetchone()
    if row and (time.time() - (row[1] or 0)) < CACHE_TTL:
        return json.loads(row[0])
    return None


def _save_cache(conn: sqlite3.Connection, character_id: int, data: list[dict]):
    conn.execute("DELETE FROM char_assets_cache WHERE character_id=?", (character_id,))
    conn.execute(
        "INSERT INTO char_assets_cache (character_id, data_json, cached_at) VALUES (?,?,?)",
        (character_id, json.dumps(data), time.time())
    )
    conn.commit()


def load_cached_container_names(conn: sqlite3.Connection,
                               item_ids) -> dict[int, str]:
    """{item_id: custom name} for whichever of `item_ids` have one.

    A plain dict rather than the (value, age) pair the other readers return:
    the caller already has a fallback for a missing name — the container's type
    — so "not synced" and "not named" collapse into the same rendering, and
    inventing a distinction the UI cannot express would be ceremony.
    """
    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    out: dict[int, str] = {}
    # Chunked: SQLite's parameter limit is 999 by default and a big account can
    # hold more containers than that.
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        ph = ",".join("?" * len(chunk))
        for item_id, name in conn.execute(
            f"SELECT item_id, name FROM container_name_cache WHERE item_id IN ({ph})",
            chunk,
        ).fetchall():
            out[item_id] = name
    return out


def save_cached_container_names(conn: sqlite3.Connection,
                                names: dict[int, str]) -> None:
    for item_id, name in names.items():
        conn.execute(
            "INSERT INTO container_name_cache (item_id, name, cached_at)"
            " VALUES (?,?,?) ON CONFLICT (item_id) DO UPDATE SET"
            " name=excluded.name, cached_at=excluded.cached_at",
            (int(item_id), name, time.time()),
        )


def _field(asset, name: str):
    """Read `name` off an asset, whichever shape it arrived in.

    Both are real and both circulate: `_parse_assets` produces `CharAsset`
    objects, while `_load_cache` and `deps._load_assets_from_cache` hand back
    the raw JSON dicts. Insisting on one here would just move the conversion to
    every caller, and the failure mode is an AttributeError from inside a
    sync tick — which is where this was found.
    """
    if isinstance(asset, dict):
        return asset.get(name)
    return getattr(asset, name, None)


def container_item_ids(assets) -> list[int]:
    """Which of these assets hold other assets.

    An item is a container exactly when something else is located *in* it, so
    this is the intersection of the two id sets — no category list to keep in
    step with CCP, and it covers assembled ships and ship maintenance bays
    without naming them.
    """
    item_ids = {_field(a, "item_id") for a in assets}
    inside = {_field(a, "location_id") for a in assets}
    return sorted(i for i in (item_ids & inside) if i)


async def fetch_container_names(client, owner_id: int, token: str, item_ids,
                                conn: sqlite3.Connection | None = None,
                                corporate: bool = False) -> dict[int, str] | None:
    """Resolve custom names for `item_ids` in one POST, and cache them.

    Only ask about items this owner holds. Posting another pilot's item_ids
    fails the whole batch — which is how the "All characters" view once ended
    up with no custom names at all and every assembled ship shown as its bare
    hull type.
    """
    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    kind = "corporations" if corporate else "characters"
    out: dict[int, str] = {}
    # ESI caps this endpoint at 1000 ids per call.
    for start in range(0, len(ids), 1000):
        chunk = ids[start:start + 1000]
        try:
            r = await client.post(
                f"{ESI_BASE}/{kind}/{owner_id}/assets/names/",
                params={"datasource": "tranquility"},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                content=json.dumps(chunk),
                timeout=15,
            )
        except Exception:
            return None if not out else out
        if r.status_code != 200:
            return None if not out else out
        for entry in r.json():
            if entry.get("name"):
                out[entry["item_id"]] = entry["name"]
    if conn is not None:
        save_cached_container_names(conn, out)
    return out


def load_cached_assets(conn: sqlite3.Connection,
                       character_id: int) -> tuple[list[CharAsset] | None, float]:
    """(assets, cached_at) from the cache at any age, or (None, 0) if absent.

    Deliberately ignores `CACHE_TTL`, which is what `_load_cache` above exists
    to enforce. The TTL is the *fetcher's* rule — "is this worth another round
    trip" — and applying it on the read path made an aged cache indistinguish-
    able from an empty one, so the page fetched. The background worker owns
    freshness now; the page's job is to say how old its answer is.

    None rather than [] for "never synced", for the reason every cache in this
    codebase now does: a hangar shown as empty when nobody has looked is a
    statement about your assets rather than about the sync.
    """
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_assets_cache WHERE character_id=?",
        (character_id,)).fetchone()
    if not row:
        return None, 0.0
    try:
        return _parse_assets(json.loads(row[0])), float(row[1] or 0.0)
    except (ValueError, TypeError, KeyError):
        return None, 0.0


def load_cached_corp_assets(conn: sqlite3.Connection,
                            corporation_id: int) -> tuple[list[CharAsset] | None, float]:
    """The same, for a corporation's hangars."""
    row = conn.execute(
        "SELECT data_json, cached_at FROM corp_assets_cache WHERE corporation_id=?",
        (corporation_id,)).fetchone()
    if not row:
        return None, 0.0
    try:
        return _parse_assets(json.loads(row[0])), float(row[1] or 0.0)
    except (ValueError, TypeError, KeyError):
        return None, 0.0


async def fetch_assets(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
    force_refresh: bool = False,
) -> list[CharAsset]:
    """Loads all of the character's assets (paginated), with caching."""
    if not force_refresh:
        cached = _load_cache(conn, character_id)
        if cached is not None:
            return _parse_assets(cached)

    headers = {"Authorization": f"Bearer {access_token}"}
    all_items: list[dict] = []
    page = 1

    while True:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/assets/",
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
    return _parse_assets(all_items)


def _parse_assets(raw: list[dict]) -> list[CharAsset]:
    result = []
    for item in raw:
        result.append(CharAsset(
            item_id            = item["item_id"],
            type_id            = item["type_id"],
            location_id        = item["location_id"],
            location_flag      = item.get("location_flag", "Hangar"),
            quantity           = item.get("quantity", 1),
            is_singleton       = item.get("is_singleton", False),
            is_blueprint_copy  = item.get("is_blueprint_copy", False),
        ))
    return result


def ensure_corp_assets_table(conn: sqlite3.Connection) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists."""
    ensure_db_schema(conn)


def _load_corp_cache(conn: sqlite3.Connection, corporation_id: int) -> list[dict] | None:
    row = conn.execute(
        "SELECT data_json, cached_at FROM corp_assets_cache WHERE corporation_id=?",
        (corporation_id,)
    ).fetchone()
    if row and (time.time() - (row[1] or 0)) < CACHE_TTL:
        return json.loads(row[0])
    return None


def _save_corp_cache(conn: sqlite3.Connection, corporation_id: int, data: list[dict]):
    conn.execute("DELETE FROM corp_assets_cache WHERE corporation_id=?", (corporation_id,))
    conn.execute(
        "INSERT INTO corp_assets_cache (corporation_id, data_json, cached_at) VALUES (?,?,?)",
        (corporation_id, json.dumps(data), time.time())
    )
    conn.commit()


async def fetch_corp_assets(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
    force_refresh: bool = False,
) -> tuple[int, list[CharAsset]]:
    """Fetch corporation assets. Returns (corp_id, assets). Empty list if no ESI access."""
    headers = {"Authorization": f"Bearer {access_token}"}
    char_r = await client.get(
        f"{ESI_BASE}/characters/{character_id}/",
        params={"datasource": "tranquility"},
        headers=headers,
        timeout=10,
    )
    char_r.raise_for_status()
    corp_id: int = char_r.json()["corporation_id"]

    if not force_refresh:
        cached = _load_corp_cache(conn, corp_id)
        if cached is not None:
            return corp_id, _parse_assets(cached)

    all_items: list[dict] = []
    page = 1

    while True:
        r = await client.get(
            f"{ESI_BASE}/corporations/{corp_id}/assets/",
            params={"datasource": "tranquility", "page": page},
            headers=headers,
            timeout=20,
        )
        if r.status_code in (401, 403):
            return corp_id, []
        r.raise_for_status()
        items = r.json()
        all_items.extend(items)

        total_pages = int(r.headers.get("x-pages", 1))
        if page >= total_pages:
            break
        page += 1

    _save_corp_cache(conn, corp_id, all_items)
    return corp_id, _parse_assets(all_items)


def assets_at_location(assets: list[CharAsset], location_id: int) -> dict[int, int]:
    """
    Returns {type_id: total_quantity} for a given station/structure.
    Ignores singletons (ships, unique items).
    """
    result: dict[int, int] = {}
    for a in assets:
        if a.location_id != location_id or a.is_singleton:
            continue
        result[a.type_id] = result.get(a.type_id, 0) + a.quantity
    return result


def assets_at_locations(
    assets: list[CharAsset], location_ids: "set[int] | list[int]"
) -> dict[int, int]:
    """
    Returns {type_id: total_quantity} aggregated across MULTIPLE stations/structures.
    Ignores singletons. Used for selecting stock sources in the production plan
    (the user checks which stations the inventory should be counted from).
    """
    wanted = set(location_ids)
    result: dict[int, int] = {}
    for a in assets:
        if a.is_singleton or a.location_id not in wanted:
            continue
        result[a.type_id] = result.get(a.type_id, 0) + a.quantity
    return result
