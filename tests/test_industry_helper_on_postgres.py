"""The station rig and ME-bonus cluster of `industry_helper`, on both backends.

**These assertions were written before the conversion and are unchanged by it.**
They started life in `test_industry_helper_rigs.py`, green against the `sqlite3`
version of the module, because seven of its nineteen functions had no test at
all — including both writers — and a write that loses its `commit()` during a
conversion passes every same-connection check and drops the row when the request
ends. Assertions written afterwards can only describe whatever the rewrite did.
These had to survive it, which is the stronger claim, and all that changed is
the fixture underneath them.

**Every test here now runs on both backends.** One of them —
`test_the_computed_percentage_stacks_multiplicatively` — did not, and the reason
is worth keeping. Four functions here (`get_station_facility`,
`get_station_te_multiplier`, `get_station_me_multiplier` and
`get_station_me_bonus_pct`) ask `location_resolver` for the system security
multiplier whenever a station actually has rigs fitted, and it still built `?`
placeholders, which psycopg does not accept. So `industry_helper` was on the
portable layer without being Postgres-clean end to end, and that one test
carried a `sqlite_only` marker naming exactly where the edge was.

Deleting that marker was the stated definition of done for the
`location_resolver` slice, and it is gone. A marker that names the next piece of
work turns out to be a better handover note than a paragraph in a worklist:
it fails loudly the moment the claim stops being true.

Both backends are seeded with the same handful of real rig types rather than a
copy of `sde_base.db`, so the two halves run on identical data and the expected
numbers are exact on either.

Postgres comes from the container in `tests/test_postgres_schema.py`; without it
those parameterisations skip and the SQLite half still runs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.web import industry_helper as ih
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_industry_helper"

# Real Standup rigs, chosen to span every branch of the bonus calculation:
# the "I" variants take the base numbers and "II" the enhanced ones; the M-set
# pair carries exactly one of ME or TE each; and the L-set rig is a plain
# "Efficiency" rig, which carries both.
ME_RIG_T1 = 43920      # Standup M-Set Equipment Manufacturing Material Efficiency I
ME_RIG_T2 = 43921      # ... Material Efficiency II
TE_RIG_T1 = 37160      # ... Time Efficiency I
TE_RIG_T2 = 37161      # ... Time Efficiency II
L_RIG_BOTH = 37170     # Standup L-Set Equipment Manufacturing Efficiency I
NOT_A_RIG = 999_111    # planted in a rig group, to exercise the name filter

SEED_TYPES = [
    (ME_RIG_T1, "Standup M-Set Equipment Manufacturing Material Efficiency I", 1816),
    (ME_RIG_T2, "Standup M-Set Equipment Manufacturing Material Efficiency II", 1816),
    (TE_RIG_T1, "Standup M-Set Equipment Manufacturing Time Efficiency I", 1819),
    (TE_RIG_T2, "Standup M-Set Equipment Manufacturing Time Efficiency II", 1819),
    (L_RIG_BOTH, "Standup L-Set Equipment Manufacturing Efficiency I", 1850),
]

STATION = 1035466617946
NPC_STATION = 60003760


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    """An engine per backend, with the app and SDE tables present and seeded.

    A file rather than `:memory:` on SQLite, because several of these tests open
    a *second* connection to check that a write actually committed — and every
    `:memory:` connection is a distinct, empty database that merely shares a
    name.
    """
    from app.db.schema import create_sde_schema

    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'eve_cache.db'}")
        with eng.connect() as c:
            from app.db.schema import apply_schema
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
        for type_id, name, group_id in SEED_TYPES:
            c.execute(
                text("INSERT INTO sde_types (type_id, name, group_id, published)"
                     " VALUES (:t, :n, :g, 1)"),
                {"t": type_id, "n": name, "g": group_id},
            )
        c.commit()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _backend(conn) -> str:
    return conn.engine.dialect.name


# ── the control ──────────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing suite: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    assert conn.execute(
        text("SELECT COUNT(*) FROM sde_types")).fetchone()[0] == len(SEED_TYPES)


# ── populate_rig_bonuses ─────────────────────────────────────────────────────

def test_populate_reads_the_rigs_out_of_the_sde(conn):
    ih.populate_rig_bonuses(conn)

    rows = dict(conn.execute(
        text("SELECT type_id, name FROM rig_bonuses")).fetchall())
    assert ME_RIG_T1 in rows, f"on {_backend(conn)}: the T1 ME rig was not imported"
    assert "Standup" in rows[ME_RIG_T1]


def test_a_tech_two_rig_carries_the_enhanced_bonus(conn):
    """2.0 vs 2.4 is the whole T1/T2 distinction, and it is computed from the
    name rather than stored, so it is exactly the kind of thing a conversion
    can drop without any statement failing."""
    ih.populate_rig_bonuses(conn)

    t1, t2 = [tuple(conn.execute(
        text("SELECT me_bonus, te_bonus FROM rig_bonuses WHERE type_id=:t"),
        {"t": r}).fetchone()) for r in (ME_RIG_T1, ME_RIG_T2)]

    assert t1 == (2.0, 0.0), f"on {_backend(conn)}"
    assert t2 == (2.4, 0.0), f"on {_backend(conn)}"


def test_a_time_efficiency_rig_carries_te_and_no_me(conn):
    """The ME and TE columns are filled from different substrings of the name.
    Swapping them would leave every row present and every number wrong."""
    ih.populate_rig_bonuses(conn)

    for rig, expected in ((TE_RIG_T1, (0.0, 20.0)), (TE_RIG_T2, (0.0, 24.0))):
        got = tuple(conn.execute(
            text("SELECT me_bonus, te_bonus FROM rig_bonuses WHERE type_id=:t"),
            {"t": rig}).fetchone())
        assert got == expected, f"on {_backend(conn)}: rig {rig}"


def test_a_plain_efficiency_rig_carries_both(conn):
    """"Material Efficiency" and "Time Efficiency" each set one column; a rig
    named just "Efficiency" sets both, and it is a third branch rather than a
    special case of either."""
    ih.populate_rig_bonuses(conn)

    got = tuple(conn.execute(
        text("SELECT me_bonus, te_bonus FROM rig_bonuses WHERE type_id=:t"),
        {"t": L_RIG_BOTH}).fetchone())

    assert got == (2.0, 20.0), f"on {_backend(conn)}"


class _CountingConn:
    """A real connection that records which statements wrote.

    `populate_rig_bonuses` used to be cheap by returning the moment the table
    had any rows. It now compares against the SDE instead, so "cheap" has to be
    demonstrated rather than assumed: an unchanged SDE must still write nothing.
    """

    def __init__(self, inner):
        self._inner = inner
        self.writes: list[str] = []

    def execute(self, stmt, *a, **kw):
        head = str(stmt).strip().split()[0].upper()
        if head in ("INSERT", "DELETE", "UPDATE"):
            self.writes.append(head)
        return self._inner.execute(stmt, *a, **kw)

    def commit(self):
        return self._inner.commit()


def test_a_rig_added_by_a_later_sde_appears(conn):
    """The defect this replaced, and the reason it was invisible.

    `rig_bonuses` is an *app* table, so an SDE import leaves it alone, and
    nothing else clears it. While `populate_rig_bonuses` returned early on "the
    table has rows", a database filled from one SDE build kept that build's rigs
    for ever — CCP adds structure rigs, and a new one would never reach the
    picker nor carry its bonus into any ME/TE figure.
    """
    ih.populate_rig_bonuses(conn)
    before = conn.execute(text("SELECT COUNT(*) FROM rig_bonuses")).fetchone()[0]

    new_rig = 99_001
    conn.execute(text("INSERT INTO sde_types (type_id, name, group_id, published)"
                      " VALUES (:t, 'Standup M-Set Equipment Manufacturing"
                      " Material Efficiency III', 1816, 1)"), {"t": new_rig})
    conn.commit()

    ih.populate_rig_bonuses(conn)

    got = conn.execute(text("SELECT me_bonus FROM rig_bonuses WHERE type_id=:t"),
                       {"t": new_rig}).fetchone()
    assert got is not None, f"a rig added after first population never appeared, on {_backend(conn)}"
    assert conn.execute(
        text("SELECT COUNT(*) FROM rig_bonuses")).fetchone()[0] == before + 1


def test_a_rig_the_sde_no_longer_publishes_is_removed(conn):
    """The same staleness in the other direction: a row left behind goes on
    being offered in the picker for a rig that no longer exists."""
    conn.execute(text("INSERT INTO rig_bonuses (type_id, name, set_size,"
                      " category, me_bonus, te_bonus)"
                      " VALUES (1, 'sentinel', 'M', 'manufacturing', 0, 0)"))
    conn.commit()

    ih.populate_rig_bonuses(conn)

    assert conn.execute(
        text("SELECT COUNT(*) FROM rig_bonuses WHERE type_id=1")).fetchone()[0] == 0


def test_nothing_is_written_when_the_sde_has_not_changed(conn):
    """What the old early return bought, kept. Comparing before writing is the
    whole point — otherwise this rewrites every rig on every settings page."""
    ih.populate_rig_bonuses(conn)

    spy = _CountingConn(conn)
    ih.populate_rig_bonuses(spy)

    assert spy.writes == [], (
        f"a second call rewrote the table on {_backend(conn)}: {spy.writes}")


def test_items_without_standup_in_the_name_are_skipped(conn):
    """CCP has put non-rig items in these groups before; the name filter is
    what keeps them out."""
    conn.execute(text("INSERT INTO sde_types (type_id, name, group_id, published)"
                      " VALUES (:t, 'Not A Rig At All', 1816, 1)"),
                 {"t": NOT_A_RIG})
    conn.commit()

    ih.populate_rig_bonuses(conn)

    assert conn.execute(
        text("SELECT COUNT(*) FROM rig_bonuses WHERE type_id=:t"),
        {"t": NOT_A_RIG}).fetchone()[0] == 0


# ── get_rig_types ────────────────────────────────────────────────────────────

def test_rig_types_are_filtered_to_the_structure(conn):
    """A Raitaru is an M-set manufacturing structure, so an L-set rig must not
    be offered for it — fitting one is impossible in game."""
    ih.populate_rig_bonuses(conn)

    offered = {r["type_id"] for r in ih.get_rig_types(conn, "raitaru")}

    assert ME_RIG_T1 in offered, f"on {_backend(conn)}"
    assert L_RIG_BOTH not in offered, "an L-set rig was offered for a Raitaru"


def test_an_unknown_structure_type_offers_nothing(conn):
    ih.populate_rig_bonuses(conn)

    assert ih.get_rig_types(conn, "not-a-structure") == []
    assert ih.get_rig_types(conn, "") == []


def test_rig_types_come_back_sorted_by_name(conn):
    """The dropdown is built straight from this order."""
    ih.populate_rig_bonuses(conn)

    names = [r["name"] for r in ih.get_rig_types(conn, "raitaru")]

    assert names == sorted(names), f"on {_backend(conn)}: {names}"
    assert len(names) > 1, "a one-item list would satisfy any ordering"


# ── save/get_station_rigs_full ───────────────────────────────────────────────

def test_a_saved_rig_configuration_reads_back(conn):
    ih.populate_rig_bonuses(conn)

    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, TE_RIG_T1, None)
    got = ih.get_station_rigs_full(conn, STATION)

    assert got["structure_type"] == "raitaru", f"on {_backend(conn)}"
    assert got["rigs"] == [ME_RIG_T1, TE_RIG_T1, None]


def test_every_fitted_rig_adds_its_own_bonus(conn):
    """Two rigs in two slots, and the structure bonus on top.

    The rewritten lookup is an expanding bindparam over the fitted ids and the
    sum runs over the slots, so this is what catches a rig silently dropping out
    of the total — a plausible number that understates the station.

    1.0 structure + 2.0 + 2.4 = 5.4.
    """
    ih.populate_rig_bonuses(conn)

    both = ih.save_station_rigs_full(conn, STATION, "raitaru",
                                     ME_RIG_T1, ME_RIG_T2, None)

    assert both == pytest.approx(5.4), f"on {_backend(conn)}"


def test_an_unconfigured_station_reads_as_empty_rather_than_missing(conn):
    """"No row" and "no rigs" have to arrive as the same shape, because the
    caller unpacks three slots either way."""
    got = ih.get_station_rigs_full(conn, NPC_STATION)

    assert got == {"me_bonus_pct": 0.0, "structure_type": None,
                   "rigs": [None, None, None]}


def test_saving_rigs_twice_replaces_rather_than_accumulates(conn):
    ih.populate_rig_bonuses(conn)

    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, ME_RIG_T2, None)
    second = ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, None, None)

    assert second == pytest.approx(3.0), f"on {_backend(conn)}"
    assert ih.get_station_rigs_full(conn, STATION)["rigs"] == [ME_RIG_T1, None, None]


def test_a_saved_rig_configuration_survives_a_new_connection(engine):
    """The lost-`commit()` net for this writer.

    SQLAlchemy opens a transaction on first use and rolls it back when the
    connection closes, where `sqlite3` in its default isolation mode commits
    some statements for itself. A converted writer that dropped its commit
    passes every assertion made on the same connection and loses the write the
    moment the request ends. Asking a *different* connection is the only version
    of this question that can fail.
    """
    with engine.connect() as c:
        ih.populate_rig_bonuses(c)
        ih.save_station_rigs_full(c, STATION, "raitaru", ME_RIG_T1, None, None)

    with engine.connect() as c:
        reread = ih.get_station_rigs_full(c, STATION)

    assert reread["structure_type"] == "raitaru", (
        f"on {engine.dialect.name}: the write did not commit")
    assert reread["rigs"] == [ME_RIG_T1, None, None]


# ── save/get_station_me_bonus ────────────────────────────────────────────────

def test_saving_an_me_bonus_does_not_wipe_the_rig_configuration(conn):
    """This was a live bug once: the writer used `INSERT OR REPLACE`, which
    deletes the row and inserts a new one, so every column the statement did not
    name came back NULL — adjusting a number un-configured the station.
    `ON CONFLICT DO UPDATE` is what fixed it, and this is the assertion that
    notices if the conversion reintroduces the old shape. It is also the one
    that would catch `OR REPLACE` surviving into a dialect that has no such
    syntax at all.
    """
    ih.populate_rig_bonuses(conn)
    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, TE_RIG_T1, None)

    ih.save_station_me_bonus(conn, STATION, 7.5)

    still = ih.get_station_rigs_full(conn, STATION)
    assert still["structure_type"] == "raitaru", (
        f"on {_backend(conn)}: the structure type was wiped")
    assert still["rigs"] == [ME_RIG_T1, TE_RIG_T1, None], "the rig slots were wiped"


def test_the_stored_me_bonus_is_clamped_to_the_sane_range(conn):
    ih.save_station_me_bonus(conn, STATION, 99.0)
    assert ih.get_station_me_bonus(conn, STATION) == 25.0, f"on {_backend(conn)}"

    ih.save_station_me_bonus(conn, STATION, -5.0)
    assert ih.get_station_me_bonus(conn, STATION) == 0.0


def test_an_unconfigured_station_has_no_stored_bonus(conn):
    assert ih.get_station_me_bonus(conn, NPC_STATION) == 0.0


def test_a_saved_me_bonus_survives_a_new_connection(engine):
    """The lost-`commit()` net for the second writer."""
    with engine.connect() as c:
        ih.save_station_me_bonus(c, STATION, 7.5)

    with engine.connect() as c:
        assert ih.get_station_me_bonus(c, STATION) == pytest.approx(7.5), (
            f"on {engine.dialect.name}: the write did not commit")


# ── the cost bonus ───────────────────────────────────────────────────────────

def test_the_structure_cost_bonus_follows_the_structure_type(conn):
    """Raitaru 3 %, Azbel 4 %, Sotiyo 5 %, refineries none. Read straight off
    the saved structure type, so it needs no security multiplier and stays on
    the portable path."""
    ih.populate_rig_bonuses(conn)

    ih.save_station_rigs_full(conn, STATION, "raitaru", None, None, None)
    assert ih.get_station_cost_bonus(conn, STATION) == pytest.approx(0.03)

    ih.save_station_rigs_full(conn, STATION, "sotiyo", None, None, None)
    assert ih.get_station_cost_bonus(conn, STATION) == pytest.approx(0.05)

    ih.save_station_rigs_full(conn, STATION, "athanor", None, None, None)
    assert ih.get_station_cost_bonus(conn, STATION) == 0.0


def test_an_unconfigured_station_has_no_cost_bonus(conn):
    assert ih.get_station_cost_bonus(conn, NPC_STATION) == 0.0


# ── the adjusted-price cache ─────────────────────────────────────────────────

def test_the_cache_only_reader_returns_what_is_stored(conn):
    """`get_adjusted_prices_cached` is the one the margin tracker calls on every
    page load, and it must never fetch."""
    conn.execute(text("INSERT INTO adjusted_price_cache (type_id, adjusted,"
                      " cached_at) VALUES (34, 5.5, 0)"))
    conn.commit()

    assert ih.get_adjusted_prices_cached(conn) == {34: 5.5}, f"on {_backend(conn)}"


def test_an_empty_adjusted_price_cache_reads_as_empty(conn):
    assert ih.get_adjusted_prices_cached(conn) == {}


# ── the computed percentage ──────────────────────────────────────────────────

def test_an_unconfigured_station_has_a_neutral_multiplier(conn):
    """No row means no rigs, and the function returns before it would ask
    `location_resolver` for anything — which is why this one runs on both."""
    assert ih.get_station_me_multiplier(conn, NPC_STATION) == 1.0
    assert ih.get_station_me_bonus_pct(conn, NPC_STATION) == 0.0


def test_the_computed_percentage_stacks_multiplicatively(conn):
    """`get_station_me_bonus` returns what was *stored* — an arithmetic sum —
    and `get_station_me_bonus_pct` returns the *combined* saving, which stacks
    multiplicatively. They are deliberately different numbers, and the UI shows
    the second one.

    Raitaru + two T1 ME rigs: 1 - (1-0.01)(1-0.02)(1-0.02) = 1 - 0.950796,
    so 4.9204 %, where the stored arithmetic sum says 5.0.

    This was SQLite-only until `location_resolver` converted: a station with
    rigs fitted sends it through `get_station_security_multiplier`, which used
    to build `?` placeholders. It runs on both backends now, which was the
    stated definition of done for that slice.
    """
    ih.populate_rig_bonuses(conn)
    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, ME_RIG_T1, None)

    assert ih.get_station_me_bonus(conn, STATION) == pytest.approx(5.0)
    assert ih.get_station_me_bonus_pct(conn, STATION) == pytest.approx(4.9204, abs=1e-4)
