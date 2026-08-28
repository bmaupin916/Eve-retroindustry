"""Market KPIs rolled up to a market group — the aggregation the tree exists for.

§9.4 of the design doc specifies eight KPIs. **Three of them are honest today**,
and this module carries only those three; the rest need a market-history fill
that has not happened (`price_history_cache` holds 0 rows). Shipping a column
that is always blank is what the reactions board already did with Sell Advantage,
and it is worse than not shipping the column.

What is available comes from `market_price_cache`, which the volume phase keeps
warm for ~19,600 types:

* **Spread** — `(sell - buy) / sell`. How far apart the two sides are; wide means
  illiquid or trader-controlled.
* **Daily volume** — `volume / 7`. The stored `volume` is a **seven-day** sum
  (`_HIST_WINDOW_DAYS` in `app/market/prices.py`), not the thirty-day mean §9.4
  asks for, because only twelve days are retained anywhere. Callers must label
  it as such. `VOLUME_WINDOW_DAYS` is exported so no caller has to remember.
* **Days of supply** — `jita_available / daily_volume`. How long the standing
  sell wall would take to clear at recent velocity. This is the group-level form
  of §9.4's "days to clear": that one is stated per *your* quantity, which has no
  meaning for a whole market group, so the market's own depth stands in for it.
  Same question — *can this absorb what I want to sell, or am I holding stock
  for a month* — asked of the market rather than of one order.

### Coverage is itself a number, and it is reported

A median margin over 3 of a group's 47 types is not a fact about the group. Every
row carries `type_count` and `priced` so a caller can say "3 of 47" rather than
implying the median describes the whole branch. This is the same discipline the
reactions board arrived at the hard way: rows whose cost is incomplete are
demoted and labelled, never quietly averaged in.

### Medians are computed in Python, deliberately

SQLite has no median and PostgreSQL spells it `percentile_cont`, so a portable
statement cannot produce one. Rather than branch on the dialect — which is the
class of thing Step 4 spent itself removing — the aggregate rows come back
per type and `statistics.median` does the rest. At the root level that is ~19,400
rows, which measures in tens of milliseconds and is a page that is browsed rather
than polled.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from sqlalchemy import text

from app.market import tree

#: The stored `volume` column is a sum over this many days, not a daily figure
#: and not the 30-day mean §9.4 specifies. Exported so the UI can label it.
VOLUME_WINDOW_DAYS = 7


@dataclass(frozen=True)
class GroupStats:
    """One market group with the KPIs that can honestly be computed for it."""
    group: tree.Group
    priced: int                          # types with a usable sell price
    median_spread_pct: float | None
    median_daily_volume: float | None
    median_days_of_supply: float | None

    @property
    def coverage(self) -> float | None:
        """Fraction of the group's types that have a price at all.

        None for an empty group: zero of zero is not zero coverage, it is a
        question that does not apply.
        """
        if not self.group.type_count:
            return None
        return self.priced / self.group.type_count


_ROWS = """
WITH RECURSIVE sub(root_id, gid) AS (
    SELECT market_group_id, market_group_id
      FROM sde_market_groups
     WHERE {parent_clause}
    UNION ALL
    SELECT s.root_id, g.market_group_id
      FROM sde_market_groups g
      JOIN sub s ON g.parent_group_id = s.gid
)
SELECT s.root_id, p.sell_price, p.buy_price, p.volume, p.jita_available
  FROM sub s
  JOIN sde_types t ON t.market_group_id = s.gid AND t.published = 1
  LEFT JOIN market_price_cache p ON p.type_id = t.type_id
"""


def _spread_pct(sell, buy) -> float | None:
    """`(sell - buy) / sell` as a percentage, or None when it is meaningless.

    A missing side is not a zero spread, and a non-positive sell price would
    divide by zero or invert the sign.
    """
    if sell is None or buy is None or sell <= 0:
        return None
    return (sell - buy) / sell * 100.0


def _days_of_supply(available, daily_volume) -> float | None:
    """How long the sell wall lasts at recent velocity.

    None when nothing trades: dividing by a zero daily volume would report
    infinity, and "nothing has traded in a week" is a different statement from
    "this will take forever to clear" — only the first is something we measured.
    """
    if available is None or not daily_volume or daily_volume <= 0:
        return None
    return available / daily_volume


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def stats_for_children(conn, parent_id: int | None = None) -> list[GroupStats]:
    """KPIs for every direct child of `parent_id`, or for the roots when None.

    One recursive pass for the tree shape and one for the numbers. Groups with
    no priced types still come back — with `None` medians and `priced` at zero —
    because a branch that vanishes from the listing is indistinguishable from a
    branch that does not exist.
    """
    groups = tree.children(conn, parent_id)
    if not groups:
        return []

    where, params = tree.parent_clause(parent_id)
    rows = conn.execute(text(_ROWS.format(parent_clause=where)), params).fetchall()

    spreads: dict[int, list[float]] = {}
    volumes: dict[int, list[float]] = {}
    supply: dict[int, list[float]] = {}
    priced: dict[int, int] = {}

    for root_id, sell, buy, volume, available in rows:
        daily = (volume / VOLUME_WINDOW_DAYS) if volume else None
        if sell is not None and sell > 0:
            priced[root_id] = priced.get(root_id, 0) + 1
        s = _spread_pct(sell, buy)
        if s is not None:
            spreads.setdefault(root_id, []).append(s)
        if daily:
            volumes.setdefault(root_id, []).append(daily)
        d = _days_of_supply(available, daily)
        if d is not None:
            supply.setdefault(root_id, []).append(d)

    return [
        GroupStats(
            group=g,
            priced=priced.get(g.group_id, 0),
            median_spread_pct=_median(spreads.get(g.group_id, [])),
            median_daily_volume=_median(volumes.get(g.group_id, [])),
            median_days_of_supply=_median(supply.get(g.group_id, [])),
        )
        for g in groups
    ]
