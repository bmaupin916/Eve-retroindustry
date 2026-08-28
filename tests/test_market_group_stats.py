"""Group-level market KPIs — `app/market/group_stats.py`, on both backends.

Seeded so every median is a middle value of an odd set and can be checked by
hand, rather than a mean of two that would also match a subtly wrong grouping.

The tree is the one from `test_market_tree_on_postgres.py` plus one more type,
with prices chosen so each KPI has a different answer:

    Alpha  -> 3 published types, all priced
       spreads 10 / 50 / 25 %      -> median 25
       daily volumes 10 / 20 / 30  -> median 20
       days of supply 5 / 20 / 10  -> median 10
    Beta   -> 2 published types, one priced with no buy side, one with no row
       every median None, coverage 1 of 2
    Gamma  -> no types at all

`Alpha Grandchild` also holds an **unpublished** type carrying deliberately
extreme prices. If the published filter ever stops applying, Alpha's spread
median moves from 25 to 37.5 and three assertions fail at once.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.market import group_stats
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_group_stats"

ALPHA, ALPHA_CHILD, ALPHA_GRANDCHILD = 1, 10, 11
BETA, BETA_CHILD = 2, 20
GAMMA = 30

SEED_GROUPS = [
    (ALPHA, None, "Alpha", 0),
    (BETA, None, "Beta", 1),
    (GAMMA, None, "Gamma", 1),
    (ALPHA_CHILD, ALPHA, "Alpha Child", 1),
    (ALPHA_GRANDCHILD, ALPHA_CHILD, "Alpha Grandchild", 0),
    (BETA_CHILD, BETA, "Beta Child", 1),
]

#: (type_id, market_group_id, published)
SEED_TYPES = [
    (100, ALPHA_GRANDCHILD, 1),
    (101, ALPHA_GRANDCHILD, 0),      # unpublished, priced absurdly on purpose
    (102, ALPHA_CHILD, 1),
    (106, ALPHA, 1),
    (103, BETA_CHILD, 1),
    (104, BETA, 1),                  # published, no price row at all
]

#: (type_id, sell, buy, volume_7d, jita_available)
SEED_PRICES = [
    (100, 100.0, 90.0, 70, 50),      # spread 10%, daily 10, supply 5
    (102, 200.0, 100.0, 140, 400),   # spread 50%, daily 20, supply 20
    (106, 400.0, 300.0, 210, 300),   # spread 25%, daily 30, supply 10
    (101, 1_000_000.0, 1.0, 700_000, 999_999),   # unpublished: must not count
    (103, 10.0, None, 0, 5),         # priced, but no buy side and nothing traded
]


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    from app.db.schema import apply_schema, create_sde_schema

    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'eve_cache.db'}")
        with eng.connect() as c:
            apply_schema(c.connection.driver_connection)
        create_sde_schema(eng)
        _seed(eng)
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
    create_sde_schema(eng)
    _seed(eng)
    yield eng
    eng.dispose()


def _seed(eng) -> None:
    with eng.connect() as c:
        for gid, parent, name, has_types in SEED_GROUPS:
            c.execute(
                text("INSERT INTO sde_market_groups"
                     " (market_group_id, parent_group_id, name, has_types)"
                     " VALUES (:g, :p, :n, :h)"),
                {"g": gid, "p": parent, "n": name, "h": has_types})
        for type_id, group, published in SEED_TYPES:
            c.execute(
                text("INSERT INTO sde_types"
                     " (type_id, name, market_group_id, published)"
                     " VALUES (:t, :n, :m, :p)"),
                {"t": type_id, "n": f"Type {type_id}", "m": group, "p": published})
        for type_id, sell, buy, volume, available in SEED_PRICES:
            c.execute(
                text("INSERT INTO market_price_cache"
                     " (type_id, sell_price, buy_price, volume, jita_available,"
                     "  cached_at) VALUES (:t, :s, :b, :v, :a, 0)"),
                {"t": type_id, "s": sell, "b": buy, "v": volume, "a": available})
        c.commit()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _by_id(rows) -> dict:
    return {r.group.group_id: r for r in rows}


def test_every_child_gets_a_row_even_with_nothing_priced(conn):
    """A branch that vanishes from the listing looks like a branch that does
    not exist, so an unpriced group is reported rather than dropped."""
    rows = group_stats.stats_for_children(conn, None)
    assert [r.group.name for r in rows] == ["Alpha", "Beta", "Gamma"]


def test_the_medians_are_the_middle_of_the_subtree(conn):
    alpha = _by_id(group_stats.stats_for_children(conn, None))[ALPHA]
    assert alpha.median_spread_pct == pytest.approx(25.0)
    assert alpha.median_daily_volume == pytest.approx(20.0)
    assert alpha.median_days_of_supply == pytest.approx(10.0)


def test_unpublished_types_do_not_move_the_medians(conn):
    """The unpublished type is priced at a million ISK against a 1 ISK buy.

    Were it counted, Alpha's spread median would be 37.5 rather than 25 — so
    this asserts the filter through its effect on a number, not by re-running
    the filter's own query.
    """
    alpha = _by_id(group_stats.stats_for_children(conn, None))[ALPHA]
    assert alpha.median_spread_pct == pytest.approx(25.0)
    assert alpha.priced == 3


def test_a_type_with_no_buy_side_is_priced_but_has_no_spread(conn):
    """Half a market is not a zero spread."""
    beta = _by_id(group_stats.stats_for_children(conn, None))[BETA]
    assert beta.priced == 1                 # type 103 only; 104 has no row
    assert beta.median_spread_pct is None


def test_nothing_traded_is_not_infinite_days_of_supply(conn):
    """Type 103 has a sell wall of 5 and a seven-day volume of zero.

    Dividing gives infinity, which would sort to the top of any "least liquid"
    ranking and read as a measurement. "Nothing traded this week" is a
    different statement and the honest one is None.
    """
    beta = _by_id(group_stats.stats_for_children(conn, None))[BETA]
    assert beta.median_days_of_supply is None
    assert beta.median_daily_volume is None


def test_coverage_says_how_much_of_the_group_the_median_describes(conn):
    rows = _by_id(group_stats.stats_for_children(conn, None))
    assert rows[ALPHA].coverage == pytest.approx(1.0)      # 3 of 3
    assert rows[BETA].coverage == pytest.approx(0.5)       # 1 of 2
    assert rows[GAMMA].coverage is None                    # 0 of 0 is not 0%


def test_an_empty_group_reports_nothing_rather_than_zero(conn):
    gamma = _by_id(group_stats.stats_for_children(conn, None))[GAMMA]
    assert gamma.group.type_count == 0
    assert gamma.priced == 0
    assert gamma.median_spread_pct is None
    assert gamma.median_daily_volume is None
    assert gamma.median_days_of_supply is None


def test_a_child_level_rolls_up_its_own_subtree(conn):
    """Descending must re-aggregate, not slice the parent's numbers.

    Alpha Child covers types 102 and 100 — daily volumes 20 and 10, median 15 —
    where Alpha's own median over three types is 20.
    """
    child = _by_id(group_stats.stats_for_children(conn, ALPHA))[ALPHA_CHILD]
    assert child.group.type_count == 2
    assert child.median_daily_volume == pytest.approx(15.0)


def test_the_volume_window_is_seven_days_and_says_so(conn):
    """The stored column is a seven-day sum, not the thirty-day mean §9.4 asks
    for. A caller that renders it as "daily volume" without the window is
    reporting a number nobody measured."""
    assert group_stats.VOLUME_WINDOW_DAYS == 7
    alpha = _by_id(group_stats.stats_for_children(conn, None))[ALPHA]
    # type 106 stores 210 over the window; 210/7 = 30 is the top of the three.
    assert alpha.median_daily_volume == pytest.approx(20.0)
