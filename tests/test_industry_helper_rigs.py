"""The station rig and ME-bonus cluster of `industry_helper`.

**Written before the conversion, deliberately.** A probe that wrapped every
function in the module and ran the whole suite found seven of them are never
executed by any test at all — `populate_rig_bonuses`, `get_rig_types`,
`save_station_rigs_full`, `get_station_rigs_full`, `get_station_me_bonus`,
`get_station_me_bonus_pct` and `save_station_me_bonus`. Two of those are
writers, and the worklist's most expensive trap is a write that loses its
`commit()` during conversion: it passes every assertion made on the same
connection and drops the row when the request ends.

So these assertions exist to be *preserved*, not to be written afterwards. They
are green against the `sqlite3` version of the module; the conversion has to
leave them green, and then this file moves onto the cross-backend fixture.

They run against a throwaway copy of the committed `sde_base.db`, because
`populate_rig_bonuses` reads real rig types out of it.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

import pytest

from app.web import industry_helper as ih

# Real Standup M-set manufacturing rigs, chosen to span both bonus branches:
# the plain "I" variants take the base numbers and the "II" variants the
# enhanced ones, and each rig carries exactly one of ME or TE.
ME_RIG_T1 = 43920      # Standup M-Set Equipment Manufacturing Material Efficiency I
ME_RIG_T2 = 43921      # ... Material Efficiency II
TE_RIG_T1 = 37160      # ... Time Efficiency I
TE_RIG_T2 = 37161      # ... Time Efficiency II
L_ME_RIG_GROUP = 1850  # an L-set manufacturing group, for the size filter

STATION = 1035466617946
NPC_STATION = 60003760

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDE = os.path.join(REPO, "sde_base.db")


@pytest.fixture
def conn(tmp_path):
    """A throwaway SDE copy with the runtime tables created."""
    path = str(tmp_path / "eve_cache.db")
    shutil.copy2(SDE, path)

    c = sqlite3.connect(path)
    ih.ensure_industry_tables(c)
    yield c
    c.close()


def _reopen(conn) -> sqlite3.Connection:
    """A second connection to the same file — the only way to ask whether a
    write actually committed."""
    path = [r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"][0]
    return sqlite3.connect(path)


# ── populate_rig_bonuses ─────────────────────────────────────────────────────

def test_populate_reads_the_rigs_out_of_the_sde(conn):
    ih.populate_rig_bonuses(conn)

    rows = dict(conn.execute("SELECT type_id, name FROM rig_bonuses").fetchall())
    assert ME_RIG_T1 in rows, "the T1 ME rig was not imported"
    assert "Standup" in rows[ME_RIG_T1]


def test_a_tech_two_rig_carries_the_enhanced_bonus(conn):
    """2.0 vs 2.4 is the whole T1/T2 distinction, and it is computed from the
    name rather than stored, so it is exactly the kind of thing a conversion
    can drop without any statement failing."""
    ih.populate_rig_bonuses(conn)

    t1, t2 = [conn.execute(
        "SELECT me_bonus, te_bonus FROM rig_bonuses WHERE type_id=?", (r,)
    ).fetchone() for r in (ME_RIG_T1, ME_RIG_T2)]

    assert t1 == (2.0, 0.0)
    assert t2 == (2.4, 0.0)


def test_a_time_efficiency_rig_carries_te_and_no_me(conn):
    """The ME and TE columns are filled from different substrings of the name.
    Swapping them would leave every row present and every number wrong."""
    ih.populate_rig_bonuses(conn)

    assert conn.execute("SELECT me_bonus, te_bonus FROM rig_bonuses WHERE type_id=?",
                        (TE_RIG_T1,)).fetchone() == (0.0, 20.0)
    assert conn.execute("SELECT me_bonus, te_bonus FROM rig_bonuses WHERE type_id=?",
                        (TE_RIG_T2,)).fetchone() == (0.0, 24.0)


def test_populate_is_a_noop_once_the_table_has_rows(conn):
    """The early return is the only thing making this cheap to call repeatedly.
    A conversion that lost it would re-read the SDE every time."""
    conn.execute("INSERT INTO rig_bonuses (type_id, name, set_size, category,"
                 " me_bonus, te_bonus) VALUES (?,?,?,?,?,?)",
                 (1, "sentinel", "M", "manufacturing", 0.0, 0.0))
    conn.commit()

    ih.populate_rig_bonuses(conn)

    assert conn.execute("SELECT COUNT(*) FROM rig_bonuses").fetchone()[0] == 1


def test_items_without_standup_in_the_name_are_skipped(conn):
    """CCP has put non-rig items in these groups before; the name filter is
    what keeps them out. No real row exercises it today, so this plants one."""
    conn.execute("INSERT INTO sde_types (type_id, name, group_id, published)"
                 " VALUES (?,?,?,?)", (999_111, "Not A Rig At All", 1816, 1))
    conn.commit()

    ih.populate_rig_bonuses(conn)

    assert conn.execute("SELECT COUNT(*) FROM rig_bonuses WHERE type_id=?",
                        (999_111,)).fetchone()[0] == 0


# ── get_rig_types ────────────────────────────────────────────────────────────

def test_rig_types_are_filtered_to_the_structure(conn):
    """A Raitaru is an M-set manufacturing structure, so an L-set rig must not
    be offered for it — fitting one is impossible in game."""
    ih.populate_rig_bonuses(conn)

    offered = {r["type_id"] for r in ih.get_rig_types(conn, "raitaru")}

    assert ME_RIG_T1 in offered
    l_set = {r[0] for r in conn.execute(
        "SELECT type_id FROM sde_types WHERE group_id=?", (L_ME_RIG_GROUP,))}
    assert not (offered & l_set), "an L-set rig was offered for a Raitaru"


def test_an_unknown_structure_type_offers_nothing(conn):
    ih.populate_rig_bonuses(conn)

    assert ih.get_rig_types(conn, "not-a-structure") == []
    assert ih.get_rig_types(conn, "") == []


def test_rig_types_come_back_sorted_by_name(conn):
    """The dropdown is built straight from this order."""
    ih.populate_rig_bonuses(conn)

    names = [r["name"] for r in ih.get_rig_types(conn, "raitaru")]

    assert names == sorted(names)
    assert len(names) > 1, "a one-item list would satisfy any ordering"


# ── save/get_station_rigs_full ───────────────────────────────────────────────

def test_a_saved_rig_configuration_reads_back(conn):
    ih.populate_rig_bonuses(conn)

    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, TE_RIG_T1, None)
    got = ih.get_station_rigs_full(conn, STATION)

    assert got["structure_type"] == "raitaru"
    assert got["rigs"] == [ME_RIG_T1, TE_RIG_T1, None]


def test_the_same_rig_in_two_slots_counts_twice(conn):
    """The one that matters most for the conversion.

    The bonus is summed over the *slot list* while the lookup is done over the
    *unique* ids, because a station can fit the same rig twice. Rewriting the
    hand-built `IN (?,?,?)` as an expanding bindparam is easy to do in a way
    that sums the unique set instead — which reads as a plausible number and
    understates the bonus.

    1.0 structure + 2.0 + 2.0 = 5.0, where deduplicating would give 3.0.
    """
    ih.populate_rig_bonuses(conn)

    both = ih.save_station_rigs_full(conn, STATION, "raitaru",
                                     ME_RIG_T1, ME_RIG_T1, None)

    assert both == pytest.approx(5.0)


def test_an_unconfigured_station_reads_as_empty_rather_than_missing(conn):
    """"No row" and "no rigs" have to arrive as the same shape, because the
    caller unpacks three slots either way."""
    got = ih.get_station_rigs_full(conn, NPC_STATION)

    assert got == {"me_bonus_pct": 0.0, "structure_type": None,
                   "rigs": [None, None, None]}


def test_saving_rigs_twice_replaces_rather_than_accumulates(conn):
    ih.populate_rig_bonuses(conn)

    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, ME_RIG_T1, None)
    second = ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, None, None)

    assert second == pytest.approx(3.0)
    assert ih.get_station_rigs_full(conn, STATION)["rigs"] == [ME_RIG_T1, None, None]


def test_a_saved_rig_configuration_survives_a_new_connection(conn):
    """The lost-`commit()` net for this writer."""
    ih.populate_rig_bonuses(conn)
    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, None, None)

    other = _reopen(conn)
    try:
        assert ih.get_station_rigs_full(other, STATION)["structure_type"] == "raitaru"
    finally:
        other.close()


# ── save/get_station_me_bonus ────────────────────────────────────────────────

def test_saving_an_me_bonus_does_not_wipe_the_rig_configuration(conn):
    """This was a live bug once: the writer used `INSERT OR REPLACE`, which
    deletes the row and re-inserts it, so every column the statement did not
    name came back NULL — adjusting a number un-configured the station.
    `ON CONFLICT DO UPDATE` is what fixed it, and this is the assertion that
    notices if the conversion reintroduces the old shape.
    """
    ih.populate_rig_bonuses(conn)
    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, TE_RIG_T1, None)

    ih.save_station_me_bonus(conn, STATION, 7.5)

    still = ih.get_station_rigs_full(conn, STATION)
    assert still["structure_type"] == "raitaru", "the structure type was wiped"
    assert still["rigs"] == [ME_RIG_T1, TE_RIG_T1, None], "the rig slots were wiped"


def test_the_stored_me_bonus_is_clamped_to_the_sane_range(conn):
    ih.save_station_me_bonus(conn, STATION, 99.0)
    assert ih.get_station_me_bonus(conn, STATION) == 25.0

    ih.save_station_me_bonus(conn, STATION, -5.0)
    assert ih.get_station_me_bonus(conn, STATION) == 0.0


def test_an_unconfigured_station_has_no_stored_bonus(conn):
    assert ih.get_station_me_bonus(conn, NPC_STATION) == 0.0


def test_a_saved_me_bonus_survives_a_new_connection(conn):
    """The lost-`commit()` net for the second writer."""
    ih.save_station_me_bonus(conn, STATION, 7.5)

    other = _reopen(conn)
    try:
        assert ih.get_station_me_bonus(other, STATION) == pytest.approx(7.5)
    finally:
        other.close()


# ── the computed percentage ──────────────────────────────────────────────────

def test_the_computed_percentage_stacks_multiplicatively(conn):
    """`get_station_me_bonus` returns what was *stored* — an arithmetic sum —
    and `get_station_me_bonus_pct` returns the *combined* saving, which stacks
    multiplicatively. They are deliberately different numbers, and the UI shows
    the second one.

    Raitaru + two T1 ME rigs: 1 - (1-0.01)(1-0.02)(1-0.02) = 1 - 0.950796,
    so 4.9204 %, where the stored arithmetic sum says 5.0.
    """
    ih.populate_rig_bonuses(conn)
    ih.save_station_rigs_full(conn, STATION, "raitaru", ME_RIG_T1, ME_RIG_T1, None)

    assert ih.get_station_me_bonus(conn, STATION) == pytest.approx(5.0)
    assert ih.get_station_me_bonus_pct(conn, STATION) == pytest.approx(4.9204, abs=1e-4)


def test_an_unconfigured_station_has_a_neutral_multiplier(conn):
    assert ih.get_station_me_multiplier(conn, NPC_STATION) == 1.0
    assert ih.get_station_me_bonus_pct(conn, NPC_STATION) == 0.0
