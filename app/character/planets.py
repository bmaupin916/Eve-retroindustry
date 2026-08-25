"""
Planetary Interaction — a character's planet colonies and extractor timers.

ESI: GET /characters/{id}/planets/            (colony list)
     GET /characters/{id}/planets/{planet_id}/ (pins incl. extractor expiry)
Scope: esi-planets.manage_planets.v1

The headline value (à la RIFT) is the extractor expiry countdown — PI is
"set and forget until the extractor program runs out", so knowing when to go
reset it is what matters.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

ESI_BASE = "https://esi.evetech.net/latest"

#: ESI said the token lacks `esi-planets.manage_planets.v1`. Distinct from "no
#: colonies": the fix is a re-login, not a command centre.
FORBIDDEN = "forbidden"

PLANET_TYPES: dict[str, str] = {
    "temperate": "Temperate", "barren": "Barren", "oceanic": "Oceanic",
    "ice": "Ice", "gas": "Gas", "lava": "Lava", "storm": "Storm", "plasma": "Plasma",
}


def planet_type_label(t: str) -> str:
    return PLANET_TYPES.get(t, (t or "").title() or "Planet")


def load_cached_colonies(conn: Connection,
                         char_id: int) -> tuple[object, float]:
    """(result, cached_at) in the shape `_fetch_pi_colonies` used to return.

    `result` is one of:
      (colonies, details) — details aligned positionally with colonies
      "forbidden"         — the token predates the PI scope
      None                — never synced

    None rather than an empty pair for "never synced", for the reason every
    cache here does it: a character shown with no colonies when nobody has
    looked is a claim about their PI, and PI is the one thing in this app you
    check *because* you expect to have forgotten about it.
    """
    row = conn.execute(
        text("SELECT status, data_json, cached_at FROM pi_colony_cache"
             " WHERE char_id=:cid"),
        {"cid": char_id}).fetchone()
    if not row:
        return None, 0.0
    status, blob, at = row[0], row[1], float(row[2] or 0.0)
    if status == FORBIDDEN:
        return FORBIDDEN, at
    try:
        payload = json.loads(blob)
        return (payload["colonies"], payload["details"]), at
    except (ValueError, TypeError, KeyError):
        return None, 0.0


def save_cached_colonies(conn: Connection, char_id: int,
                         colonies, details, status: str = "ok") -> None:
    # No commit: the caller owns the transaction boundary.
    conn.execute(
        text("INSERT INTO pi_colony_cache (char_id, status, data_json, cached_at)"
             " VALUES (:cid, :status, :data, :cached_at)"
             " ON CONFLICT (char_id) DO UPDATE SET"
             " status=excluded.status, data_json=excluded.data_json,"
             " cached_at=excluded.cached_at"),
        {"cid": char_id, "status": status,
         "data": json.dumps({"colonies": colonies or [], "details": details or []}),
         "cached_at": time.time()},
    )


def load_planet_names(conn: Connection, planet_ids) -> dict[int, str]:
    """Whatever names are already known. Never fetches."""
    ids = [int(p) for p in planet_ids]
    if not ids:
        return {}
    out: dict[int, str] = {}
    # Chunked, because a statement caps how many parameters it may bind and an
    # expanding bindparam still binds one per id. That cap is a compile-time
    # setting — 999 before SQLite 3.32, far higher on the build here, 65,535 on
    # Postgres — so 900 is under all of them and the cost below the limit is one
    # extra round trip per 900 planets.
    stmt = text("SELECT planet_id, name FROM planet_name_cache"
                " WHERE planet_id IN :ids").bindparams(
                    bindparam("ids", expanding=True))
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        for pid, name in conn.execute(stmt, {"ids": chunk}).fetchall():
            if name:
                out[pid] = name
    return out


async def fetch_planet_names(client: httpx.AsyncClient, conn: Connection,
                             planet_ids) -> dict[int, str]:
    """Resolve any planet names not already cached, and store them.

    Planet names are permanent — "Jita IV" will be "Jita IV" forever — so a
    name is fetched at most once per database, ever. That is what makes this
    safe to do from the worker rather than on a page: there is no staleness to
    manage, only a first sight.

    `/universe/names/` cannot resolve planets, hence the per-planet endpoint
    and one call per unknown id. Unauthenticated: planet names are public.
    """
    known = load_planet_names(conn, planet_ids)
    missing = [int(p) for p in planet_ids if int(p) not in known]
    if not missing:
        return known

    async def _one(pid: int):
        try:
            r = await client.get(f"{ESI_BASE}/universe/planets/{pid}/",
                                 params={"datasource": "tranquility"}, timeout=8)
            if r.status_code == 200:
                return pid, r.json().get("name")
        except Exception:
            pass
        return pid, None

    for pid, name in await asyncio.gather(*[_one(p) for p in missing]):
        if name:
            known[pid] = name
            conn.execute(
                text("INSERT INTO planet_name_cache (planet_id, name)"
                     " VALUES (:pid, :name)"
                     " ON CONFLICT (planet_id) DO UPDATE SET name=excluded.name"),
                {"pid": pid, "name": name})
    return known


async def fetch_colonies(client: httpx.AsyncClient, char_id: int, token: str,
                         conn: Connection | None = None):
    """The colony list and every colony's detail, in one go.

    This is the whole PI fetch: one call for the list, then one per planet.
    Returns the same three-way result `load_cached_colonies` describes, and
    caches anything conclusive — including "forbidden", which is a durable fact
    about the token rather than a transient failure and would otherwise be
    re-discovered on every tick.

    A detail call that fails leaves `None` in its slot rather than dropping the
    colony, because the slots are positional: dropping one would silently
    re-pair every colony after it with another planet's pins.
    """
    colonies = await fetch_planets(client, char_id, token)
    if colonies == FORBIDDEN:
        if conn is not None:
            save_cached_colonies(conn, char_id, [], [], FORBIDDEN)
        return FORBIDDEN
    if colonies is None:
        return None                     # transient: leave the last good cache
    if not colonies:
        if conn is not None:
            save_cached_colonies(conn, char_id, [], [])
        return [], []

    details = await asyncio.gather(*[
        fetch_planet_detail(client, char_id, c["planet_id"], token)
        for c in colonies], return_exceptions=True)
    details = [d if isinstance(d, dict) else None for d in details]
    if conn is not None:
        save_cached_colonies(conn, char_id, colonies, details)
        # Names for any planet this database has not seen before. One call each,
        # once ever — and doing it here is what lets /planets read them without
        # a fetch of its own.
        try:
            await fetch_planet_names(client, conn,
                                     [c["planet_id"] for c in colonies])
        except Exception:
            pass                        # a nameless planet renders as its id
    return colonies, details


async def fetch_planets(client: httpx.AsyncClient, char_id: int, token: str):
    """Colony list for a character. Returns the list on success, the string
    "forbidden" if the token lacks the PI scope (so the caller can prompt a
    re-login), or None on any other error."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/planets/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 403:
            return FORBIDDEN
    except Exception:
        pass
    return None


async def fetch_planet_detail(client: httpx.AsyncClient, char_id: int,
                              planet_id: int, token: str) -> dict | None:
    """Colony detail: {links, pins, routes}. Extractor pins carry
    `extractor_details` + `expiry_time`; factory pins carry `factory_details`."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/planets/{planet_id}/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None
