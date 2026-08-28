"""§9.4's history-backed KPIs: volatility, trend, competition — and a real
thirty-day volume at last.

`app/market/group_stats.py` carries the three KPIs that need only the current
price snapshot. These are the three that need the daily series, which nothing
filled until v0.9.86. With `market_stats` materialising them, a market group of
four hundred types costs one indexed read rather than four hundred JSON parses
per page load.

### The window is thirty calendar days, anchored on the series

ESI's history omits days with no trades, so "the last thirty records" can span
half a year for an illiquid item and calling that a thirty-day figure would be
false. The window is therefore thirty **calendar** days, and it is anchored on
the newest date in the series rather than on the clock: the same series always
produces the same numbers, which is what makes these testable without a clock
and what stops a recompute changing a figure nobody's data changed.

`days` records how many trading days fell inside that window. It is not
bookkeeping — it is the difference between a volatility computed from thirty
observations and one computed from three, and a consumer that cannot tell them
apart will print them side by side as equals.

### The definitions

* **Volatility** — standard deviation of the daily average price over the
  window, as a percentage of its mean. Population stdev, not sample: this is the
  whole window, not a sample drawn from it.
* **Trend** — the last seven days' mean average price against the whole
  window's, as a percentage. Positive means rising into your build time.
* **Competition** — mean `order_count` per day. §9.4 phrases it as "how
  contested is the sell wall".
* **Average daily volume** — mean `vol` across the window. This is the figure
  §9.4 actually specified; `group_stats` has been standing in with a seven-day
  sum because twelve days was all that was retained anywhere.

Every one returns `None` rather than a number when the input cannot support it —
an empty window, a zero mean, fewer than two observations for a standard
deviation. A zero would be a measurement, and none of these has been measured.
"""
from __future__ import annotations

import datetime
import json
import statistics
import time

from sqlalchemy import bindparam, text

#: §9.4 quotes every KPI over thirty days.
WINDOW_DAYS = 30

#: The short leg of the trend comparison.
TREND_DAYS = 7

#: Below this many trading days a figure is not reported at group level.
#: Not arbitrary: a week is the shortest span over which "volatility" and
#: "trend" mean anything, and an item that traded three days in thirty has
#: a number that would sit in a median beside one computed from thirty as
#: though they were the same measurement. The per-type row keeps its figure
#: either way — `days` is right there — this only gates the rollup.
MIN_STAT_DAYS = 7


def _as_date(value: str) -> datetime.date | None:
    try:
        year, month, day = (int(part) for part in value.split("-"))
        return datetime.date(year, month, day)
    except Exception:                       # noqa: BLE001 — a malformed day
        return None


def window(series: list[dict], days: int = WINDOW_DAYS) -> list[dict]:
    """The entries within `days` calendar days of the newest one.

    Anchored on the series rather than on `today`, so the same input always
    gives the same answer. Staleness is `computed_at`'s job, not this one's.
    """
    dated: list[tuple[dict, datetime.date]] = []
    for entry in series:
        if not isinstance(entry, dict) or not isinstance(entry.get("d"), str):
            continue
        day = _as_date(entry["d"])
        if day is not None:
            dated.append((entry, day))
    if not dated:
        return []
    newest = max(day for _entry, day in dated)
    cutoff = newest - datetime.timedelta(days=days - 1)
    return [entry for entry, day in sorted(dated, key=lambda pair: pair[1])
            if day >= cutoff]


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _numbers(entries: list[dict], key: str) -> list[float]:
    return [e[key] for e in entries
            if isinstance(e.get(key), (int, float))]


def volatility_pct(entries: list[dict]) -> float | None:
    """Population stdev of the daily average, over its own mean.

    Needs two observations: the standard deviation of one number is zero, which
    would report the single most illiquid items in the game as the most stable.
    """
    prices = _numbers(entries, "avg")
    if len(prices) < 2:
        return None
    mean = statistics.fmean(prices)
    if mean <= 0:
        return None
    return statistics.pstdev(prices) / mean * 100.0


def trend_pct(entries: list[dict], short_days: int = TREND_DAYS) -> float | None:
    """Recent mean price against the window's mean, as a percentage."""
    prices = _numbers(entries, "avg")
    if len(prices) < 2:
        return None
    overall = statistics.fmean(prices)
    if overall <= 0:
        return None
    recent = statistics.fmean(prices[-short_days:])
    return (recent - overall) / overall * 100.0


def compute(series: list[dict], days: int = WINDOW_DAYS) -> dict:
    """Every stat for one type's series. Values are None when unsupported."""
    entries = window(series, days)
    return {
        "days": len(entries),
        "avg_daily_volume": _mean(_numbers(entries, "vol")),
        "volatility_pct": volatility_pct(entries),
        "trend_pct": trend_pct(entries),
        "avg_order_count": _mean(_numbers(entries, "orders")),
    }


def store(conn, region_id: int, type_id: int, stats: dict) -> None:
    conn.execute(
        text("INSERT INTO market_stats (region_id, type_id, days,"
             " avg_daily_volume, volatility_pct, trend_pct, avg_order_count,"
             " computed_at)"
             " VALUES (:rid, :tid, :days, :vol, :volat, :trend, :orders, :at)"
             " ON CONFLICT (region_id, type_id) DO UPDATE SET"
             " days=excluded.days, avg_daily_volume=excluded.avg_daily_volume,"
             " volatility_pct=excluded.volatility_pct,"
             " trend_pct=excluded.trend_pct,"
             " avg_order_count=excluded.avg_order_count,"
             " computed_at=excluded.computed_at"),
        {"rid": region_id, "tid": type_id, "days": stats["days"],
         "vol": stats["avg_daily_volume"], "volat": stats["volatility_pct"],
         "trend": stats["trend_pct"], "orders": stats["avg_order_count"],
         "at": time.time()},
    )


def stale_type_ids(conn, region_id: int) -> list[int]:
    """Types whose history has moved since their stats were computed.

    A left join rather than a timestamp sweep: a type with history and no stats
    row at all is exactly as stale as one whose history was refetched, and both
    have to come back.
    """
    rows = conn.execute(
        text("SELECT h.type_id FROM price_history_cache h"
             " LEFT JOIN market_stats s"
             "   ON s.region_id = h.region_id AND s.type_id = h.type_id"
             " WHERE h.region_id = :rid"
             "   AND (s.computed_at IS NULL OR s.computed_at < h.cached_at)"),
        {"rid": region_id},
    ).fetchall()
    return [r[0] for r in rows]


def refresh(conn, region_id: int, type_ids: list[int] | None = None) -> int:
    """Recompute stats for stale types. Returns how many rows were written."""
    targets = stale_type_ids(conn, region_id) if type_ids is None else list(type_ids)
    if not targets:
        return 0

    rows = conn.execute(
        text("SELECT type_id, data_json FROM price_history_cache"
             " WHERE region_id = :rid AND type_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"rid": region_id, "ids": targets},
    ).fetchall()

    written = 0
    for type_id, data_json in rows:
        try:
            series = json.loads(data_json) if data_json else []
        except (TypeError, ValueError):
            continue
        if not isinstance(series, list):
            continue
        store(conn, region_id, type_id, compute(series))
        written += 1
    if written:
        conn.commit()
    return written
