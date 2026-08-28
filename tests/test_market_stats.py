"""§9.4's history-backed KPIs — `app/market/stats.py`.

Numbers are chosen so every expected value is computable by hand. A KPI test
whose expectation came from running the code is a test that the code still does
what it did, which is not the claim being made.

The window is the part most likely to be got wrong twice. ESI omits days with no
trades, so "the last thirty records" can span half a year for an illiquid item —
`test_the_window_is_calendar_days_not_record_count` is the assertion that keeps
"30-day volatility" meaning thirty days.
"""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import create_engine, text

from app.market import stats
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_market_stats"
REGION = 10000002
TYPE_ID = 34


def _day(date: str, avg: float, vol: int = 100, orders: int = 5) -> dict:
    """One stored day, in the shape `fetch_region_history` produces."""
    return {"d": date, "avg": avg, "low": avg, "high": avg,
            "vol": vol, "orders": orders}


# Ten consecutive days at a steady 100 ISK: no spread, so no volatility.
FLAT = [_day(f"2026-08-{d:02d}", 100.0) for d in range(1, 11)]


def test_a_flat_price_has_no_volatility():
    assert stats.volatility_pct(FLAT) == pytest.approx(0.0)


def test_volatility_is_the_spread_as_a_share_of_the_mean():
    """Two days at 90 and 110: mean 100, population stdev 10, so 10%."""
    series = [_day("2026-08-01", 90.0), _day("2026-08-02", 110.0)]
    assert stats.volatility_pct(series) == pytest.approx(10.0)


def test_one_observation_is_not_a_standard_deviation():
    """The stdev of a single number is zero, which would rank the most
    illiquid items in the game as the most stable."""
    assert stats.volatility_pct([_day("2026-08-01", 100.0)]) is None


def test_volatility_of_a_free_item_is_unmeasurable_not_zero():
    series = [_day("2026-08-01", 0.0), _day("2026-08-02", 0.0)]
    assert stats.volatility_pct(series) is None


def test_a_rising_price_trends_up():
    """Nine days at 100 and one at 200.

    Window mean = 1100 / 10 = 110. The last seven days are six at 100 and one
    at 200, so 800 / 7 = 114.2857. (114.2857 - 110) / 110 * 100 = 3.896%.
    """
    series = [_day(f"2026-08-{d:02d}", 100.0) for d in range(1, 10)]
    series.append(_day("2026-08-10", 200.0))
    assert stats.trend_pct(series) == pytest.approx(3.896104, rel=1e-4)


def test_a_flat_price_has_no_trend():
    assert stats.trend_pct(FLAT) == pytest.approx(0.0)


def test_the_window_is_calendar_days_not_record_count():
    """ESI omits days with no trades.

    Four records spanning a year would be "the last thirty records" and is not
    thirty days of anything. Only the two inside the window may count.
    """
    series = [_day("2025-01-01", 1.0), _day("2025-06-01", 2.0),
              _day("2026-08-01", 3.0), _day("2026-08-20", 4.0)]
    kept = stats.window(series, days=30)
    assert [e["d"] for e in kept] == ["2026-08-01", "2026-08-20"]


def test_the_window_is_anchored_on_the_series_not_the_clock():
    """The same input must always give the same numbers.

    Anchoring on today would make a recompute change a figure when nobody's
    data changed, and would make every test here depend on a clock.
    """
    old = [_day("2020-01-%02d" % d, 100.0) for d in range(1, 11)]
    assert len(stats.window(old, days=30)) == 10


def test_days_counts_trading_days_not_calendar_days():
    """The honesty column. Three trades in a thirty-day window is `days = 3`,
    and a consumer that cannot see that will print its volatility beside one
    computed from thirty as though they were the same measurement."""
    series = [_day("2026-08-01", 10.0), _day("2026-08-14", 12.0),
              _day("2026-08-20", 11.0)]
    assert stats.compute(series)["days"] == 3


def test_average_daily_volume_is_the_thirty_day_figure_9_4_asked_for():
    """`group_stats` stands in with a seven-day sum because twelve days was all
    that was retained. With history there is a real one."""
    series = [_day("2026-08-01", 10.0, vol=100), _day("2026-08-02", 10.0, vol=300)]
    assert stats.compute(series)["avg_daily_volume"] == pytest.approx(200.0)


def test_competition_is_the_mean_order_count():
    series = [_day("2026-08-01", 10.0, orders=4), _day("2026-08-02", 10.0, orders=8)]
    assert stats.compute(series)["avg_order_count"] == pytest.approx(6.0)


def test_an_empty_series_measures_nothing_rather_than_zero():
    computed = stats.compute([])
    assert computed["days"] == 0
    assert computed["avg_daily_volume"] is None
    assert computed["volatility_pct"] is None
    assert computed["trend_pct"] is None
    assert computed["avg_order_count"] is None


def test_a_malformed_day_is_skipped_not_fatal():
    """One bad record must not lose the other twenty-nine."""
    series = [_day("2026-08-01", 10.0), {"d": "not-a-date", "avg": 1.0},
              {"avg": 2.0}, _day("2026-08-02", 12.0)]
    assert stats.compute(series)["days"] == 2


# ── storage ──────────────────────────────────────────────────────────────────

@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    if request.param == "sqlite":
        from app.db.schema import apply_schema

        eng = create_engine(f"sqlite:///{tmp_path / 'eve_cache.db'}")
        with eng.connect() as c:
            apply_schema(c.connection.driver_connection)
        yield eng
        eng.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

    from app.db.migrate import upgrade_to_head

    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.commit()
    admin.dispose()

    scoped = PG_URL + ("&" if "?" in PG_URL else "?") + \
        f"options=-csearch_path%3D{PG_SCHEMA}"
    upgrade_to_head(scoped)
    eng = create_engine(scoped)
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _seed_history(conn, series, cached_at=None):
    conn.execute(
        text("INSERT INTO price_history_cache (region_id, type_id, data_json,"
             " cached_at) VALUES (:r, :t, :j, :c)"),
        {"r": REGION, "t": TYPE_ID, "j": json.dumps(series),
         "c": cached_at if cached_at is not None else time.time()},
    )
    conn.commit()


def test_a_type_with_history_and_no_stats_is_stale(conn):
    _seed_history(conn, FLAT)
    assert stats.stale_type_ids(conn, REGION) == [TYPE_ID]


def test_refresh_writes_the_computed_row(conn):
    _seed_history(conn, FLAT)
    assert stats.refresh(conn, REGION) == 1

    row = conn.execute(
        text("SELECT days, avg_daily_volume, volatility_pct FROM market_stats"
             " WHERE region_id = :r AND type_id = :t"),
        {"r": REGION, "t": TYPE_ID}).fetchone()
    assert row[0] == 10
    assert row[1] == pytest.approx(100.0)
    assert row[2] == pytest.approx(0.0)


def test_a_refreshed_type_stops_being_stale(conn):
    _seed_history(conn, FLAT)
    stats.refresh(conn, REGION)
    assert stats.stale_type_ids(conn, REGION) == []


def test_newer_history_makes_it_stale_again(conn):
    """The whole point of the timestamp comparison."""
    _seed_history(conn, FLAT)
    stats.refresh(conn, REGION)
    conn.execute(
        text("UPDATE price_history_cache SET cached_at = :c"
             " WHERE region_id = :r AND type_id = :t"),
        {"c": time.time() + 60, "r": REGION, "t": TYPE_ID})
    conn.commit()
    assert stats.stale_type_ids(conn, REGION) == [TYPE_ID]


def test_refresh_commits(conn, engine):
    _seed_history(conn, FLAT)
    stats.refresh(conn, REGION)
    with engine.connect() as other:
        assert other.execute(text("SELECT COUNT(*) FROM market_stats")).scalar() == 1


def test_unparseable_history_is_skipped_rather_than_crashing_the_round(conn):
    conn.execute(
        text("INSERT INTO price_history_cache (region_id, type_id, data_json,"
             " cached_at) VALUES (:r, :t, 'not json', :c)"),
        {"r": REGION, "t": TYPE_ID, "c": time.time()})
    conn.commit()
    assert stats.refresh(conn, REGION) == 0
