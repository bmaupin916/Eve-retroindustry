"""
Market orders — active and historical, for character and corporation.

ESI endpoints:
  Character:
    GET /characters/{id}/orders/           → active orders
    GET /characters/{id}/orders/history/   → last ~90 days (paginated)
  Corporation (requires role Accountant/Trader → otherwise 403):
    GET /corporations/{id}/orders/
    GET /corporations/{id}/orders/history/

Scope: esi-markets.read_character_orders.v1 / esi-markets.read_corporation_orders.v1
"""
from __future__ import annotations

import json
import time

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

ESI_BASE = "https://esi.evetech.net/latest"

#: Owner kinds and states, spelled once. They are part of the cache key, so a
#: typo at one call site would write a row nothing ever reads — silently, since
#: a miss is indistinguishable from "not synced yet".
CHARACTER, CORPORATION = "character", "corporation"
ACTIVE, HISTORY = "active", "history"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── cache ────────────────────────────────────────────────────────────────────

def load_cached_orders(conn: Connection, owner_id: int,
                       kind: str = CHARACTER,
                       state: str = ACTIVE) -> tuple[list[dict] | None, float]:
    """(orders, cached_at), or (None, 0) if this owner has never been synced.

    `None` and `[]` mean different things and the page renders them
    differently: nobody has looked yet, versus looked and found nothing. The
    /jobs cache learned this the hard way — conflating them makes the page
    claim a character is idle when it has simply not been reached.
    """
    row = conn.execute(
        text("SELECT data_json, cached_at FROM market_orders_cache"
             " WHERE owner_id=:owner_id AND owner_kind=:kind AND state=:state"),
        {"owner_id": owner_id, "kind": kind, "state": state},
    ).fetchone()
    if not row:
        return None, 0.0
    try:
        return json.loads(row[0]), float(row[1] or 0.0)
    except (ValueError, TypeError):
        return None, 0.0


def save_cached_orders(conn: Connection, owner_id: int, orders: list[dict],
                       kind: str = CHARACTER, state: str = ACTIVE) -> None:
    # No commit, deliberately: the caller owns the transaction boundary. All
    # three parts of the primary key are the conflict target — drop one and
    # this does not raise, it inserts a second row, and a corporation's book
    # can then appear as the pilot's own.
    conn.execute(
        text("INSERT INTO market_orders_cache"
             " (owner_id, owner_kind, state, data_json, cached_at)"
             " VALUES (:owner_id, :kind, :state, :data, :cached_at)"
             " ON CONFLICT (owner_id, owner_kind, state) DO UPDATE SET"
             " data_json=excluded.data_json, cached_at=excluded.cached_at"),
        {"owner_id": owner_id, "kind": kind, "state": state,
         "data": json.dumps(orders), "cached_at": time.time()},
    )


async def _get_all(client: httpx.AsyncClient, url: str, token: str,
                   pages: int = 5) -> list[dict] | None:
    """Paginated GET. None if the *first* page failed, so the caller can tell
    "ESI is unavailable" from "this character has no order history".

    A later page failing still returns what arrived: a partial history is worth
    more than none, and the alternative is discarding four good pages because
    the fifth timed out.
    """
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            r = await client.get(url, params={"page": page}, headers=_auth(token), timeout=15)
        except Exception:
            return None if page == 1 else out
        if r.status_code != 200:
            return None if page == 1 else out
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if page >= int(r.headers.get("x-pages", 1)):
            break
    return out


async def fetch_orders(client, char_id: int, token: str,
                       conn: Connection | None = None) -> list[dict] | None:
    """Active orders of a character (single page).

    Returns None when the fetch could not run, so the worker can leave the last
    good cache in place rather than overwriting it with an empty list — which
    would read on the page as "you have no orders" every time ESI hiccuped.
    """
    try:
        r = await client.get(f"{ESI_BASE}/characters/{char_id}/orders/",
                             headers=_auth(token), timeout=15)
        if r.status_code == 200:
            orders = r.json()
            if conn is not None:
                save_cached_orders(conn, char_id, orders, CHARACTER, ACTIVE)
            return orders
    except Exception:
        pass
    return None


async def fetch_orders_history(client, char_id: int, token: str,
                               conn: Connection | None = None) -> list[dict] | None:
    orders = await _get_all(
        client, f"{ESI_BASE}/characters/{char_id}/orders/history/", token)
    if orders is None:
        return None
    if conn is not None:
        save_cached_orders(conn, char_id, orders, CHARACTER, HISTORY)
    return orders


async def fetch_corp_orders(client, corp_id: int, token: str,
                            conn: Connection | None = None
                            ) -> tuple[list[dict] | None, str | None]:
    try:
        r = await client.get(f"{ESI_BASE}/corporations/{corp_id}/orders/",
                             params={"page": 1}, headers=_auth(token), timeout=15)
        if r.status_code == 200:
            out = r.json()
            for page in range(2, int(r.headers.get("x-pages", 1)) + 1):
                rp = await client.get(f"{ESI_BASE}/corporations/{corp_id}/orders/",
                                     params={"page": page}, headers=_auth(token), timeout=15)
                if rp.status_code != 200:
                    break
                out.extend(rp.json())
            if conn is not None:
                save_cached_orders(conn, corp_id, out, CORPORATION, ACTIVE)
            return out, None
        if r.status_code == 403:
            return None, "This character lacks the corporation role to read market orders (Accountant / Trader)."
        return None, f"ESI returned HTTP {r.status_code}."
    except Exception as exc:
        return None, str(exc)


async def fetch_corp_orders_history(client, corp_id: int, token: str,
                                    conn: Connection | None = None
                                    ) -> list[dict] | None:
    orders = await _get_all(
        client, f"{ESI_BASE}/corporations/{corp_id}/orders/history/", token)
    if orders is None:
        return None
    if conn is not None:
        save_cached_orders(conn, corp_id, orders, CORPORATION, HISTORY)
    return orders
