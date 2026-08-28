"""The market tree read model — `app/market/tree.py`.

The table it reads has been populated since the SDE subset was built and had no
consumers at all, so nothing here is a regression guard: these are the first
assertions that the tree is walked correctly.

Two of them exist because the SDE's own `hasTypes` flag is wrong in both
directions — 54 groups claim types and have no published ones, 2 deny types and
have them — so a module that trusted the flag would offer empty branches and
hide real ones. `test_a_group_that_claims_types_but_has_none_counts_zero` and
its opposite pin that the counts come from `sde_types` instead.

Fixture ids are from the committed `sde_base.db`. Where a number would drift
with a future SDE build the assertion is on the *direction* rather than the
magnitude, except for the two totals that check themselves against the database
in the same test.
"""
from __future__ import annotations

import pytest
from sqlalchemy import bindparam, text

from app.db.conn import connect
from app.market import tree

# Group 4, "Ships": no types filed directly against it, several hundred beneath.
SHIPS = 4
# has_types=1, and not one published type anywhere under it.
MICRO = 604
# has_types=0, and 42 published types under it.
CRIMINAL_EVIDENCE = 614


@pytest.fixture()
def conn():
    c = connect()
    try:
        yield c
    finally:
        c.close()


def test_the_roots_are_the_top_of_the_market_window(conn):
    roots = tree.roots(conn)
    assert len(roots) == 19
    assert all(g.parent_id is None for g in roots)
    assert [g.name for g in roots] == sorted(g.name for g in roots)


def test_type_count_is_the_whole_subtree_not_just_direct_children(conn):
    """A parent's count is the size of the market it represents.

    Ships has nothing filed directly against it, so a direct-children count
    would render the largest branch in the game as empty.
    """
    direct = conn.execute(
        text("SELECT COUNT(*) FROM sde_types"
             " WHERE market_group_id = :g AND published = 1"),
        {"g": SHIPS},
    ).scalar()
    assert direct == 0

    ships = [g for g in tree.roots(conn) if g.group_id == SHIPS]
    assert ships, "Ships is not a root any more — fix the fixture, not the test"
    assert ships[0].type_count > 100


def test_a_group_that_claims_types_but_has_none_counts_zero(conn):
    """`hasTypes` is the SDE's claim, not a measurement. 54 groups are wrong."""
    micro = _find(conn, MICRO)
    assert micro.has_types is True          # the flag says yes
    assert micro.type_count == 0            # the types say otherwise


def test_a_group_that_denies_types_but_has_them_still_counts_them(conn):
    """The opposite error, which is the one that would hide a real branch."""
    evidence = _find(conn, CRIMINAL_EVIDENCE)
    assert evidence.has_types is False      # the flag says no
    assert evidence.type_count > 0          # the types say otherwise


def test_every_published_type_is_reachable_from_exactly_one_root(conn):
    """Totals across the 19 roots must account for the whole market, once.

    This is the check that a double-counting join or a lost branch fails: the
    tree has one parent per node, so the roots partition it.
    """
    total = conn.execute(
        text("SELECT COUNT(*) FROM sde_types"
             " WHERE published = 1 AND market_group_id IS NOT NULL"),
    ).scalar()
    assert sum(g.type_count for g in tree.roots(conn)) == total


def test_children_of_a_leaf_is_empty_not_an_error(conn):
    assert tree.children(conn, MICRO) == []


def test_path_runs_root_first_and_ends_at_the_group_asked_for(conn):
    path = tree.path(conn, CRIMINAL_EVIDENCE)
    assert path, "no breadcrumb for a group that exists"
    assert path[0].parent_id is None
    assert path[-1].group_id == CRIMINAL_EVIDENCE
    # Each step is the parent of the next, which is what makes it a path
    # rather than a set that happens to contain the right ids.
    for parent, child in zip(path, path[1:]):
        assert child.parent_id == parent.group_id


def test_path_of_an_unknown_group_is_empty_not_a_stub(conn):
    """A stale bookmark should get no path, not a one-element path to nothing."""
    assert tree.path(conn, -1) == []


def test_subtree_includes_the_group_itself(conn):
    ids = tree.subtree_ids(conn, SHIPS)
    assert SHIPS in ids
    assert len(ids) > 1
    assert len(ids) == len(set(ids)), "a group appears twice — the walk revisits"


def test_subtree_of_an_unknown_group_is_empty(conn):
    assert tree.subtree_ids(conn, -1) == []


def test_type_ids_agrees_with_the_count_children_reports(conn):
    """The two functions walk the tree separately; they must not disagree."""
    ships = _find(conn, SHIPS)
    assert len(tree.type_ids(conn, SHIPS)) == ships.type_count


def test_unpublished_types_are_excluded_and_that_filter_does_work(conn):
    """Both halves matter.

    Asserting only that everything returned is published passes trivially if
    the subtree happens to contain no unpublished types at all, so the second
    assertion proves the filter is removing something real.
    """
    ids = tree.type_ids(conn, SHIPS)
    groups = tree.subtree_ids(conn, SHIPS)
    unfiltered = conn.execute(
        text("SELECT COUNT(*) FROM sde_types WHERE market_group_id IN :g")
        .bindparams(bindparam("g", expanding=True)),
        {"g": groups},
    ).scalar()
    assert unfiltered > len(ids), "nothing unpublished under Ships — pick another fixture"

    published = conn.execute(
        text("SELECT COUNT(*) FROM sde_types"
             " WHERE type_id IN :t AND published = 1")
        .bindparams(bindparam("t", expanding=True)),
        {"t": ids},
    ).scalar()
    assert published == len(ids)


def _find(conn, group_id: int) -> tree.Group:
    """One group as its parent's listing renders it, so counts are populated."""
    path = tree.path(conn, group_id)
    assert path, f"group {group_id} is gone from the SDE — fix the fixture"
    parent = path[-2].group_id if len(path) > 1 else None
    for g in tree.children(conn, parent):
        if g.group_id == group_id:
            return g
    raise AssertionError(f"group {group_id} is not among its own parent's children")
