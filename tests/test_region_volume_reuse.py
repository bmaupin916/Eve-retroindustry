"""A stored 7-day volume is reused only while ESI still calls it current.

`_cached_region_volume` exists so a custom station in an already-loaded region
need not refetch ~19k histories. What it reuses is a precomputed 7-day **sum**,
and a sum cannot be re-windowed: the moment ESI rebuilds its history the window
has moved, the total is wrong, and nothing about the stored value says so. It
was being reused verbatim for ever.

The freshness test is ESI's own `Expires`, already recorded per type in
`market_hist_etag` — the same header `_region_history_volume` uses to skip a
refetch outright. No invented TTL: a number is current while the server says it
is, and not one second longer.

This matters because the failure is silent and one-directional in effect — the
volume column simply reads wrong, and vol/7d is what a user judges liquidity by
before committing ISK to a build.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, text

from app.db.schema import apply_schema, apply_sde_schema
from app.market import prices as p

THE_FORGE = p.JITA_REGION
CURSE = 10000012
TRITANIUM = 34


@pytest.fixture
def conn(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'v.db'}")
    with engine.connect() as c:
        apply_schema(c.connection.driver_connection)
        apply_sde_schema(c.connection.driver_connection)
        yield c
    engine.dispose()


def _store_volume(conn, region_id, volume=1234):
    """A cached region volume, the way a Jita or hub refresh leaves one."""
    if region_id == THE_FORGE:
        conn.execute(text(
            "INSERT INTO market_price_cache (type_id, sell_price, buy_price,"
            " volume, cached_at) VALUES (:t, 5.0, 4.0, :v, :now)"),
            {"t": TRITANIUM, "v": volume, "now": time.time()})
    else:
        conn.execute(text(
            "INSERT INTO hub_price_cache (region_id, type_id, sell_price,"
            " buy_price, volume, cached_at)"
            " VALUES (:r, :t, 5.0, 4.0, :v, :now)"),
            {"r": region_id, "t": TRITANIUM, "v": volume, "now": time.time()})
    conn.commit()


def _store_expiry(conn, region_id, expires_at):
    conn.execute(text(
        "INSERT INTO market_hist_etag (region_id, type_id, etag, days_json,"
        " cached_at, expires_at)"
        " VALUES (:r, :t, 'x', '{}', :now, :exp)"),
        {"r": region_id, "t": TRITANIUM, "now": time.time(), "exp": expires_at})
    conn.commit()


@pytest.mark.parametrize("region", [THE_FORGE, CURSE],
                         ids=["jita-market_price_cache", "hub-hub_price_cache"])
def test_a_current_volume_is_reused(region, conn):
    """The optimisation still has to work — both storage paths, because Jita's
    volume lands in `market_price_cache` and a hub's in `hub_price_cache`."""
    _store_volume(conn, region, volume=777)
    _store_expiry(conn, region, time.time() + 3600)

    got = p._cached_region_volume(conn, region)

    assert got == {TRITANIUM: 777}


@pytest.mark.parametrize("region", [THE_FORGE, CURSE],
                         ids=["jita-market_price_cache", "hub-hub_price_cache"])
def test_an_expired_volume_is_refused(region, conn):
    """ESI has rebuilt its history, so the stored sum covers a window that has
    moved. `None` sends the caller down the slow path, which is the right
    trade: a correct number slowly beats a wrong one instantly."""
    _store_volume(conn, region, volume=777)
    _store_expiry(conn, region, time.time() - 1)

    assert p._cached_region_volume(conn, region) is None


def test_a_region_with_no_recorded_expiry_is_refused(conn):
    """Freshness that cannot be established is not freshness. Before the fix
    this was the *common* case and it reused the sum anyway."""
    _store_volume(conn, CURSE, volume=777)

    assert p._cached_region_volume(conn, CURSE) is None


def test_the_earliest_expiry_in_the_region_decides(conn):
    """`MIN(expires_at)`. Expiry is recorded per type, and ESI rebuilds a
    region's history as a unit, so one expired entry means the window has moved
    for all of them. Taking the latest instead would serve stale sums whenever
    a single type had been refetched more recently."""
    _store_volume(conn, CURSE, volume=777)
    _store_expiry(conn, CURSE, time.time() + 3600)
    conn.execute(text(
        "INSERT INTO market_hist_etag (region_id, type_id, etag, days_json,"
        " cached_at, expires_at) VALUES (:r, 35, 'y', '{}', :now, :exp)"),
        {"r": CURSE, "now": time.time(), "exp": time.time() - 1})
    conn.commit()

    assert p._cached_region_volume(conn, CURSE) is None


def test_no_region_is_refused_without_touching_the_database(conn):
    """The pre-existing guard, kept: `region_id` is optional at the call site."""
    assert p._cached_region_volume(conn, None) is None
    assert p._cached_region_volume(conn, 0) is None
