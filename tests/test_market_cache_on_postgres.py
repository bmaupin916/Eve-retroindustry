"""The market price/volume caches in `app/market/prices.py`, on both backends.

A reachability survey over the prices cluster found **ten functions no test
calls at all**. Five of them are here — the per-type price cache, the history
ETag store and the region volume reuse — and they are the ones with real
semantics rather than plumbing: a TTL that decides whether a cached price is
served, an upsert keyed on a compound key, and a region scope that decides
which hub's numbers you see.

That survey measured *reachability*, not coverage, and the distinction earned
its keep in the PI unit: renaming `target_choices` was "caught" by thirteen
tests, while blanking its return value was caught by none. A crash is not
coverage. These are real assertions on returned values.

Written against the portable query layer, so they fail on the pre-conversion
code and describe what the rewrite has to preserve. Running on both backends is
the point: every statement here is either an upsert with `excluded.*` or a
scoped read, and `market_hist_etag`'s upsert is keyed on a **compound** key,
which is the shape most likely to be got wrong once.

Two module-level caches make this file's ordering matter, and both are cleared
per test rather than trusted: `_hist_etags` and `_hist_etags_dirty` live for the
life of the process, so a test that asserts on them while another test's entries
are still resident is asserting on history — the same shape as the
prepared-statement cache that hid the route-jump chunking bug.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, text

from app.market import prices as market
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_market_cache"

TRITANIUM = 34
PYERITE = 35
JITA_REGION = 10000002          # The Forge — read from market_price_cache
HEIMATAR = 10000030             # a hub region — read from hub_price_cache
SINQ_LAISON = 10000032          # a second hub region, so the scope is decidable


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    from app.db.migrate import upgrade_to_head

    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'market.db'}"
        upgrade_to_head(url)
        eng = create_engine(url)
        yield eng
        eng.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

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
    """Empty caches, in the database and in the module.

    Cleared *before* rather than after: a test that dies half way must not leave
    rows — or in-memory ETag entries — for the next one to read.
    """
    market._hist_etags.clear()
    market._hist_etags_dirty.clear()
    with engine.connect() as c:
        for table in ("market_hist_etag", "market_price_cache", "hub_price_cache"):
            c.execute(text(f"DELETE FROM {table}"))
        c.commit()
        yield c
    market._hist_etags.clear()
    market._hist_etags_dirty.clear()


# ── the per-type price cache, and the TTL that governs it ────────────────────

def test_a_saved_price_reads_back(conn):
    market._save_cached_price(conn, TRITANIUM, 5.5, 4.5)

    assert market._get_cached_price(conn, TRITANIUM) == (5.5, 4.5)


def test_an_unknown_type_reads_back_as_a_pair_of_nones(conn):
    """Not an exception and not a single None — callers unpack two values."""
    assert market._get_cached_price(conn, 999999) == (None, None)


def test_a_price_older_than_the_ttl_is_not_served(conn):
    """`PRICE_CACHE_TTL` is 12 hours, and it is the whole reason this is a cache
    rather than a table.

    Backdated by a day, so the row exists and is *stale*. A reader that only
    checked for the row's presence would serve yesterday's price forever and
    never refetch — no error, no empty state, just a number that stopped
    moving.
    """
    market._save_cached_price(conn, TRITANIUM, 5.5, 4.5)
    conn.execute(text("UPDATE market_price_cache SET cached_at=:t WHERE type_id=:tid"),
                 {"t": time.time() - 86400, "tid": TRITANIUM})
    conn.commit()

    assert market._get_cached_price(conn, TRITANIUM) == (None, None)


def test_a_price_inside_the_ttl_is_served(conn):
    """The positive control for the test above. Without it, a `_get_cached_price`
    that returned `(None, None)` unconditionally would pass every staleness
    assertion in this file."""
    market._save_cached_price(conn, TRITANIUM, 5.5, 4.5)
    conn.execute(text("UPDATE market_price_cache SET cached_at=:t WHERE type_id=:tid"),
                 {"t": time.time() - 60, "tid": TRITANIUM})
    conn.commit()

    assert market._get_cached_price(conn, TRITANIUM) == (5.5, 4.5)


def test_saving_the_same_type_twice_updates_rather_than_duplicating(conn):
    """`ON CONFLICT (type_id) DO UPDATE`. Two rows for one type would make the
    answer depend on which the reader saw first."""
    market._save_cached_price(conn, TRITANIUM, 5.5, 4.5)
    market._save_cached_price(conn, TRITANIUM, 9.0, 8.0)

    rows = conn.execute(text(
        "SELECT sell_price, buy_price FROM market_price_cache"
        " WHERE type_id=:tid"), {"tid": TRITANIUM}).fetchall()

    assert len(rows) == 1, f"the upsert inserted a second row: {rows}"
    assert tuple(rows[0]) == (9.0, 8.0)


def test_the_optional_volume_columns_round_trip(conn):
    """`volume` and `jita_available` default to None and are what
    `_cached_region_volume` reads back — pinned here so the column order in the
    upsert cannot drift unnoticed."""
    market._save_cached_price(conn, TRITANIUM, 5.5, 4.5, volume=1234, jita_available=7)

    row = conn.execute(text(
        "SELECT volume, jita_available FROM market_price_cache"
        " WHERE type_id=:tid"), {"tid": TRITANIUM}).fetchone()

    assert tuple(row) == (1234, 7)


# ── the history ETag store ───────────────────────────────────────────────────

def test_flushed_etags_load_back(conn):
    """The round trip, through the module-level dict both halves use.

    `flush_hist_etags` writes whatever is marked dirty; `load_hist_etags` reads a
    region's rows back into memory. The pair is what stops a volume phase
    re-downloading a year of history it already has.
    """
    key = (HEIMATAR, TRITANIUM)
    market._hist_etags[key] = ("etag-abc", {"2026-01-01": 10}, time.time() + 3600)
    market._hist_etags_dirty.add(key)

    assert market.flush_hist_etags(conn) == 1

    market._hist_etags.clear()
    assert market.load_hist_etags(conn, HEIMATAR) == 1
    etag, days, _expires = market._hist_etags[key]
    assert etag == "etag-abc"
    assert days == {"2026-01-01": 10}


def test_flushing_nothing_writes_nothing(conn):
    """The `if not _hist_etags_dirty: return 0` guard. It is redundant under
    `sqlite3`, whose `executemany` no-ops on an empty sequence, and becomes
    load-bearing on the portable layer, where SQLAlchemy raises
    `StatementError` on an empty parameter list — the same transition
    `save_route_jumps` went through in v0.9.68."""
    assert market.flush_hist_etags(conn) == 0

    assert conn.execute(text("SELECT COUNT(*) FROM market_hist_etag")).fetchone()[0] == 0


def test_etags_are_scoped_to_their_region(conn):
    """Two regions, one type. `load_hist_etags` takes a region and must load only
    that region's rows — with one region in the fixture, "loaded the right ones"
    and "loaded everything" are the same observation."""
    for region in (HEIMATAR, SINQ_LAISON):
        key = (region, TRITANIUM)
        market._hist_etags[key] = (f"etag-{region}", {}, 0.0)
        market._hist_etags_dirty.add(key)
    market.flush_hist_etags(conn)
    market._hist_etags.clear()

    loaded = market.load_hist_etags(conn, HEIMATAR)

    assert loaded == 1, f"expected one region's ETags, got {loaded}"
    assert (HEIMATAR, TRITANIUM) in market._hist_etags
    assert (SINQ_LAISON, TRITANIUM) not in market._hist_etags


def test_reflushing_a_region_and_type_updates_it(conn):
    """`ON CONFLICT (region_id, type_id)` — a **compound** key, which is the
    shape most likely to be written with one column by mistake. Keyed on
    `region_id` alone, the second flush here would overwrite a different type's
    row; on `type_id` alone it would collide across regions."""
    key = (HEIMATAR, TRITANIUM)
    market._hist_etags[key] = ("first", {}, 0.0)
    market._hist_etags_dirty.add(key)
    market.flush_hist_etags(conn)

    market._hist_etags[key] = ("second", {}, 0.0)
    market._hist_etags_dirty.add(key)
    market.flush_hist_etags(conn)

    rows = conn.execute(text(
        "SELECT etag FROM market_hist_etag WHERE region_id=:r AND type_id=:t"),
        {"r": HEIMATAR, "t": TRITANIUM}).fetchall()
    assert len(rows) == 1, f"the compound-key upsert duplicated: {rows}"
    assert rows[0][0] == "second"


def test_an_entry_with_no_etag_is_not_loaded(conn):
    """`if not etag: continue`. An empty ETag cannot be sent as
    `If-None-Match`, so loading it would produce a request claiming a validator
    it does not have."""
    conn.execute(
        text("INSERT INTO market_hist_etag"
             " (region_id, type_id, etag, days_json, cached_at, expires_at)"
             " VALUES (:r, :t, :e, :d, :c, :x)"),
        {"r": HEIMATAR, "t": TRITANIUM, "e": "", "d": "{}", "c": time.time(), "x": 0.0})
    conn.commit()

    assert market.load_hist_etags(conn, HEIMATAR) == 0


# ── the region volume reuse ──────────────────────────────────────────────────

def _still_current(conn, region_id) -> None:
    """Record that ESI still calls this region's history current.

    A precondition of reuse since v0.9.82: the stored `volume` is a precomputed
    7-day *sum* and cannot be re-windowed, so it is served only while ESI's own
    `Expires` says the copy it came from is still authoritative. The tests below
    are about *which table* and *which region* a volume is read from, which is
    orthogonal — so they state the freshness rather than depend on the absence
    of a check. `tests/test_region_volume_reuse.py` is where the expiry rule
    itself is pinned.
    """
    conn.execute(
        text("INSERT INTO market_hist_etag"
             " (region_id, type_id, etag, days_json, cached_at, expires_at)"
             " VALUES (:r, :t, '', '{}', :c, :x)"),
        {"r": region_id, "t": TRITANIUM, "c": time.time(),
         "x": time.time() + 3600})
    conn.commit()


def test_the_forge_volume_comes_from_the_jita_cache(conn):
    """Jita's region is special-cased: its 7-day volumes live in
    `market_price_cache`, not `hub_price_cache`."""
    market._save_cached_price(conn, TRITANIUM, 5.5, 4.5, volume=999)
    _still_current(conn, JITA_REGION)

    assert market._cached_region_volume(conn, JITA_REGION) == {TRITANIUM: 999}


def test_a_hub_volume_comes_from_the_hub_cache_for_that_region(conn):
    """Two regions with the same type, so the `WHERE region_id=` is decidable.
    Reusing another region's volume would quietly quote the wrong market."""
    conn.execute(
        text("INSERT INTO hub_price_cache"
             " (region_id, type_id, sell_price, buy_price, volume, cached_at)"
             " VALUES (:r, :t, :s, :b, :v, :c)"),
        [{"r": HEIMATAR, "t": TRITANIUM, "s": 6.0, "b": 5.0, "v": 111,
          "c": time.time()},
         {"r": SINQ_LAISON, "t": TRITANIUM, "s": 7.0, "b": 6.0, "v": 222,
          "c": time.time()}])
    conn.commit()
    _still_current(conn, HEIMATAR)
    _still_current(conn, SINQ_LAISON)

    assert market._cached_region_volume(conn, HEIMATAR) == {TRITANIUM: 111}
    assert market._cached_region_volume(conn, SINQ_LAISON) == {TRITANIUM: 222}


def test_a_region_with_nothing_cached_is_none_not_empty(conn):
    """`None` means "not loaded yet, go and fetch"; `{}` would mean "loaded, and
    this region trades nothing". The caller branches on it, so collapsing the
    two turns a cache miss into a claim about the market.

    The freshness stamp is deliberate: without it this would return `None` at
    the expiry gate and never reach the empty-rows path it is named after —
    still green, and testing nothing.
    """
    _still_current(conn, HEIMATAR)

    assert market._cached_region_volume(conn, HEIMATAR) is None


def test_no_region_at_all_is_none(conn):
    """The `if not region_id` guard — a custom station whose region was never
    resolved."""
    assert market._cached_region_volume(conn, None) is None


def test_rows_without_a_volume_are_excluded(conn):
    """`WHERE volume IS NOT NULL`, and here it is load-bearing rather than
    decorative: the values become a `{type_id: volume}` map the caller does
    arithmetic on, so a `None` reaching it is a `TypeError` rather than a
    missing entry.
    """
    market._save_cached_price(conn, TRITANIUM, 5.5, 4.5, volume=999)
    market._save_cached_price(conn, PYERITE, 3.0, 2.0)          # no volume
    _still_current(conn, JITA_REGION)

    got = market._cached_region_volume(conn, JITA_REGION)

    assert got == {TRITANIUM: 999}, f"a null-volume row got through: {got}"
