"""The cached price-history series has one producer and two readers. Tie them.

`fetch_region_history` writes `{d, avg, low, high, vol, orders}`. It has stored
`vol` since v0.8.70 and has never stored `volume` — and `margins._avg_day_volume`
asked for `volume`, so every cached day read as zero. Worse than a zero: a list
of zeros is truthy, so it returned `0.0` instead of falling through to the
seven-day figure it has a fallback for.

Nothing failed, because `price_history_cache` is empty until somebody opens a
price chart — and the test that covered it seeded `{date, volume}`, the raw ESI
shape, which no writer in this codebase produces. The test agreed with the
reader, the reader disagreed with the writer, and the only thing that would have
noticed was the market-history fill that §9.4 step 4 is about to do.

So the test here never hand-writes the series. It calls the producer, stores what
the producer returned, and reads it back through the consumer. A key renamed on
either side fails immediately, which a hand-written fixture cannot do.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text

from app.manufacturing.margins import JITA_REGION, _avg_day_volume
from app.market.prices import fetch_region_history

TYPE_ID = 34        # Tritanium

#: One day exactly as ESI returns it, field names and all.
ESI_DAY = {
    "date": "2026-08-01",
    "average": 10.5,
    "lowest": 9.0,
    "highest": 11.0,
    "volume": 500,
    "order_count": 42,
}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    """The two calls `fetch_region_history` makes of an httpx client."""

    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, **kwargs):
        return _Resp(self._payload)


def _produced(days: list[dict]) -> list[dict]:
    return asyncio.run(fetch_region_history(_Client(days), JITA_REGION, TYPE_ID))


@pytest.fixture
def conn(tmp_path):
    from app.db.schema import apply_schema

    eng = create_engine(f"sqlite:///{tmp_path / 'eve_cache.db'}")
    with eng.connect() as c:
        apply_schema(c.connection.driver_connection)
        yield c
    eng.dispose()


def test_the_fetcher_keeps_every_field_the_kpis_need():
    series = _produced([ESI_DAY])
    assert series == [{"d": "2026-08-01", "avg": 10.5, "low": 9.0, "high": 11.0,
                       "vol": 500, "orders": 42}]


def test_order_count_survives_the_fetch():
    """§9.4's Competition KPI is `order_count`, and it was being discarded.

    Kept now rather than after the bulk fill, because adding it later means
    re-downloading a year of history for every type that was filled without it.
    """
    assert _produced([ESI_DAY])[0]["orders"] == 42


def test_a_day_esi_omits_a_field_for_does_not_lose_the_key():
    """Missing counts are zero; a missing key is a hole in the series."""
    sparse = _produced([{"date": "2026-08-02", "average": 1.0}])
    assert sparse[0]["vol"] == 0 and sparse[0]["orders"] == 0


def test_the_reader_reads_the_keys_the_writer_writes(conn):
    """The assertion whose absence let a key mismatch live in main.

    Nothing here is hand-written: the producer's own output is what gets
    stored, and the consumer's answer is checked against the input the producer
    was given. Rename a key on either side and this fails.
    """
    days = [dict(ESI_DAY, date=f"2026-08-0{d}", volume=500) for d in range(1, 8)]
    series = _produced(days)

    conn.execute(
        text("INSERT INTO price_history_cache (region_id, type_id, data_json,"
             " cached_at) VALUES (:r, :t, :j, 0)"),
        {"r": JITA_REGION, "t": TYPE_ID, "j": json.dumps(series)},
    )
    conn.commit()

    assert _avg_day_volume(conn, TYPE_ID) == pytest.approx(500.0)


def test_a_series_it_cannot_read_falls_back_instead_of_reporting_zero(conn):
    """The second half of the defect, and the one that made it invisible.

    A series whose entries carry no readable volume must fall through to the
    seven-day figure. Returning 0.0 is a measurement nobody made, and it
    suppresses the fallback that exists for exactly this case.
    """
    conn.execute(
        text("INSERT INTO market_price_cache (type_id, sell_price, volume,"
             " cached_at) VALUES (:t, 1.0, 700, 0)"),
        {"t": TYPE_ID},
    )
    conn.execute(
        text("INSERT INTO price_history_cache (region_id, type_id, data_json,"
             " cached_at) VALUES (:r, :t, :j, 0)"),
        {"r": JITA_REGION, "t": TYPE_ID,
         "j": json.dumps([{"d": "2026-08-01", "avg": 1.0}])},
    )
    conn.commit()

    assert _avg_day_volume(conn, TYPE_ID) == pytest.approx(100.0)   # 700 / 7


def test_a_genuine_no_trade_day_still_counts_as_zero(conn):
    """Skipping unreadable entries must not also skip real zeros.

    A day that traded nothing is a measurement and belongs in the mean; six
    days at 700 and one at 0 average 600, not 700.
    """
    days = [dict(ESI_DAY, date=f"2026-08-0{d}", volume=700) for d in range(1, 7)]
    days.append(dict(ESI_DAY, date="2026-08-07", volume=0))
    series = _produced(days)

    conn.execute(
        text("INSERT INTO price_history_cache (region_id, type_id, data_json,"
             " cached_at) VALUES (:r, :t, :j, 0)"),
        {"r": JITA_REGION, "t": TYPE_ID, "j": json.dumps(series)},
    )
    conn.commit()

    assert _avg_day_volume(conn, TYPE_ID) == pytest.approx(600.0)


def test_only_one_place_writes_the_history_table():
    """One writer, and this is what keeps it that way.

    The key mismatch above survived because the reader and the writer were in
    different files and nothing compared them. A second INSERT site is how that
    returns — two writers drift, and the drift is silent until a KPI reads zero.
    """
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parents[1]
    sites = []
    for path in sorted((repo / "app").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"INSERT\s+INTO\s+price_history_cache", src, re.I):
            line = src[:m.start()].count("\n") + 1
            sites.append(f"{path.relative_to(repo).as_posix()}:{line}")

    assert len(sites) == 1, f"more than one writer of price_history_cache: {sites}"
    assert sites[0].startswith("app/market/history_fill.py"), sites


def test_an_illiquid_item_does_not_report_inflated_daily_volume(conn):
    """The window is thirty calendar days, not thirty records.

    ESI omits days with no trades. Thirty records for an item that trades once
    a week span seven months, and averaging them reports "avg units traded per
    day" — the /margins column — as seven times what it is. This is the case
    where the number decides something, so it is the case it must get right.
    """
    # One trade a week for thirty weeks, 700 units each. Only the trades inside
    # the last thirty days may count: at weekly cadence that is five of them.
    import datetime

    # Volumes differ across the boundary on purpose: 25 quiet weeks at 100,
    # then 5 busy ones at 1000. The calendar window sees only the busy five and
    # answers 1000; averaging all thirty records answers 250. Equal volumes
    # would give the same mean either way and prove nothing.
    start = datetime.date(2026, 1, 5)
    days = [dict(ESI_DAY, date=(start + datetime.timedelta(weeks=w)).isoformat(),
                 volume=100 if w < 25 else 1000) for w in range(30)]
    series = _produced(days)

    conn.execute(
        text("INSERT INTO price_history_cache (region_id, type_id, data_json,"
             " cached_at) VALUES (:r, :t, :j, 0)"),
        {"r": JITA_REGION, "t": TYPE_ID, "j": json.dumps(series)},
    )
    conn.commit()

    from app.market.stats import window

    inside = window(series, 30)
    assert len(inside) == 5, f"expected five weekly trades in thirty days, got {len(inside)}"

    # 1000 is the windowed answer. 250 is what averaging all thirty records
    # gives, and is the number this test exists to reject.
    assert _avg_day_volume(conn, TYPE_ID) == pytest.approx(1000.0)
