"""Filling `price_history_cache` in the background, for the types that earn it.

§9.4's KPIs — volatility, trend, competition — all need the daily series, and
until now the only thing that wrote one was a user opening a price chart. The
table held **zero rows**. This is the fill.

### It does not sweep

19,667 types have a market group and market history is a public, per-IP route.
Sweeping them is both rude and pointless: nobody looks at 19,667 items. §9.4
names the priority order and this implements it — **the watchlist first, then
what is actually being built**:

1. `margin_watchlist` — items the user picked out by hand
2. `project_plans.product_type_id` — what they are building
3. `project_shopping.type_id` — the inputs they have to buy for it

§9.4 also lists "groups actually being browsed". That is deliberately **not**
here: recording what a user browsed means a tenth per-user table with no owner
column, added weeks before Step 5 re-keys the nine that already exist. It is a
cheap addition afterwards and an expensive one now.

### Budget, not a queue

`FILL_BUDGET` types per tick. A worker round is minutes apart, so the fill
trickles rather than bursts, and a fresh install reaches a useful state over
hours instead of hammering ESI once. Types already fresh are not re-fetched, so
a settled install spends nothing.

### One writer

`store_region_history` is the only thing in the codebase that writes this table.
That is the point rather than tidiness: the reader and writer of this series
disagreed about a key name from v0.8.70 to v0.9.85 — `vol` against `volume` —
and every cached day read as zero. Two writers is how that returns.
"""
from __future__ import annotations

import json
import time

from sqlalchemy import bindparam, text

#: The Forge. History is per-region and every KPI in §9.4 is quoted against
#: Jita, so one region is what gets filled.
JITA_REGION = 10000002

#: ESI recomputes market history about once a day. Twenty hours refreshes a type
#: daily without pinning it to a fixed hour, which would make every type in the
#: install come due in the same minute for ever.
HISTORY_FILL_TTL = 60 * 60 * 20

#: Types fetched per worker round. Small on purpose — see the module docstring.
FILL_BUDGET = 20

#: The priority order §9.4 asks for, most deserving first. Each entry is a
#: statement selecting `type_id`; they are unioned in order and de-duplicated
#: with the first occurrence winning.
_CANDIDATE_SOURCES = (
    ("watchlist", "SELECT type_id FROM margin_watchlist WHERE type_id IS NOT NULL"),
    ("plans", "SELECT product_type_id FROM project_plans"
              " WHERE product_type_id IS NOT NULL"),
    ("shopping", "SELECT type_id FROM project_shopping WHERE type_id IS NOT NULL"),
)


def store_region_history(conn, region_id: int, type_id: int, series: list[dict]) -> None:
    """The only writer of `price_history_cache`. See the module docstring."""
    conn.execute(
        text("INSERT INTO price_history_cache"
             " (region_id, type_id, data_json, cached_at)"
             " VALUES (:rid, :tid, :data_json, :cached_at)"
             " ON CONFLICT (region_id, type_id) DO UPDATE SET"
             " data_json = excluded.data_json, cached_at = excluded.cached_at"),
        {"rid": region_id, "tid": type_id,
         "data_json": json.dumps(series), "cached_at": time.time()},
    )


def candidate_type_ids(conn) -> list[int]:
    """Every type worth having history for, best first, without duplicates.

    Order is the whole value here: the budget is small, so what comes first is
    what gets filled today.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for _label, sql in _CANDIDATE_SOURCES:
        for (type_id,) in conn.execute(text(sql)):
            if type_id is not None and type_id not in seen:
                seen.add(type_id)
                ordered.append(type_id)
    return ordered


def types_needing_history(conn, limit: int = FILL_BUDGET,
                          region_id: int = JITA_REGION,
                          now: float | None = None) -> list[int]:
    """The next `limit` candidates with no history, or history past its TTL.

    Returns fewer than `limit`, or nothing at all, when everything is fresh —
    which is the steady state and costs one query per round.
    """
    candidates = candidate_type_ids(conn)
    if not candidates:
        return []

    cutoff = (now if now is not None else time.time()) - HISTORY_FILL_TTL
    fresh = {
        r[0] for r in conn.execute(
            text("SELECT type_id FROM price_history_cache"
                 " WHERE region_id = :rid AND cached_at > :cutoff"
                 "   AND type_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"rid": region_id, "cutoff": cutoff, "ids": candidates},
        )
    }
    return [t for t in candidates if t not in fresh][:limit]


async def fill_history(client, conn, limit: int = FILL_BUDGET,
                       region_id: int = JITA_REGION) -> int:
    """Fetch and store history for the next `limit` types. Returns how many.

    A type whose fetch fails is skipped and stays due, so the next round tries
    it again. Nothing is written for a failure: an empty series stored under a
    fresh timestamp would look like "this item has never traded" and suppress
    the retry, which is the same class of error as `fetch_industry_jobs`
    returning `None` rather than `[]`.
    """
    from app.market.prices import fetch_region_history

    stored = 0
    for type_id in types_needing_history(conn, limit, region_id):
        series = await fetch_region_history(client, region_id, type_id)
        if series is None:
            continue
        store_region_history(conn, region_id, type_id, series)
        stored += 1
    if stored:
        conn.commit()
    return stored
