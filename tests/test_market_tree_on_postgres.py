"""The market tree walk, on both backends.

`app/market/tree.py` is the first recursive CTE in this codebase. `WITH
RECURSIVE` is standard in SQLite and PostgreSQL both, which is exactly the kind
of confidence Step 4 spent itself disproving — SQLite's case-insensitive `LIKE`
and its inverted NULL ordering were also "standard" until they were measured. So
the walk is measured.

`tests/test_market_tree.py` asserts properties of the **real** SDE — that
`hasTypes` is wrong in both directions, that the 19 roots partition the market —
and can only run where `sde_base.db` is. This file is the complement: a nine-row
synthetic tree seeded identically on either engine, so every expected number is
exact and any disagreement between backends is the walk rather than the data.

The tree, with published type counts in brackets:

    Alpha  (root, hasTypes=0)          -> 2
      Alpha Child (hasTypes=1)         -> 2
        Alpha Grandchild (hasTypes=0)  -> 1
    Beta   (root, hasTypes=1)          -> 2
      Beta Child (hasTypes=1)          -> 1
    Gamma  (root, hasTypes=1)          -> 0

Gamma claims types and has none; Alpha and Alpha Grandchild deny them and have
them. Both errors are real in the SDE and both are reproduced here.

Postgres comes from the container in `tests/test_postgres_schema.py`; without it
that parameterisation skips and the SQLite half still runs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.market import tree
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_market_tree"

ALPHA, ALPHA_CHILD, ALPHA_GRANDCHILD = 1, 10, 11
BETA, BETA_CHILD = 2, 20
GAMMA = 30

#: (market_group_id, parent_group_id, name, has_types)
SEED_GROUPS = [
    (ALPHA, None, "Alpha", 0),
    (BETA, None, "Beta", 1),
    (GAMMA, None, "Gamma", 1),
    (ALPHA_CHILD, ALPHA, "Alpha Child", 1),
    (ALPHA_GRANDCHILD, ALPHA_CHILD, "Alpha Grandchild", 0),
    (BETA_CHILD, BETA, "Beta Child", 1),
]

#: (type_id, name, market_group_id, published)
SEED_TYPES = [
    (100, "Deep Published", ALPHA_GRANDCHILD, 1),
    (101, "Deep Unpublished", ALPHA_GRANDCHILD, 0),
    (102, "Mid Published", ALPHA_CHILD, 1),
    (103, "Beta Leaf", BETA_CHILD, 1),
    (104, "Filed On A Root", BETA, 1),
    (105, "Root Unpublished", ALPHA, 0),
]


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    from app.db.schema import create_sde_schema

    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'eve_cache.db'}")
        create_sde_schema(eng)
        _seed(eng)
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
                {"g": gid, "p": parent, "n": name, "h": has_types},
            )
        for type_id, name, group, published in SEED_TYPES:
            c.execute(
                text("INSERT INTO sde_types"
                     " (type_id, name, market_group_id, published)"
                     " VALUES (:t, :n, :m, :p)"),
                {"t": type_id, "n": name, "m": group, "p": published},
            )
        c.commit()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _by_id(groups) -> dict:
    return {g.group_id: g for g in groups}


def test_roots_are_the_null_parents_in_name_order(conn):
    """`parent_group_id IS NULL` — the clause `= :pid` cannot express.

    Worth pinning on both: a backend that quietly matched NULL with `=` would
    make the SQLite half pass and nothing else.
    """
    roots = tree.roots(conn)
    assert [g.name for g in roots] == ["Alpha", "Beta", "Gamma"]
    assert all(g.parent_id is None for g in roots)


def test_subtree_totals_roll_up_through_every_level(conn):
    """Alpha's two published types are both below it, one of them two deep."""
    roots = _by_id(tree.roots(conn))
    assert roots[ALPHA].type_count == 2
    assert roots[BETA].type_count == 2      # one on the root itself, one below
    assert roots[GAMMA].type_count == 0


def test_unpublished_types_are_excluded_on_both_backends(conn):
    """Two of the six seeded types are unpublished, at two different depths."""
    assert sorted(tree.type_ids(conn, ALPHA)) == [100, 102]
    assert sorted(tree.type_ids(conn, BETA)) == [103, 104]


def test_has_types_is_reported_but_never_believed(conn):
    roots = _by_id(tree.roots(conn))
    # Claims types, has none.
    assert roots[GAMMA].has_types is True and roots[GAMMA].type_count == 0
    # Denies types, has two beneath it.
    assert roots[ALPHA].has_types is False and roots[ALPHA].type_count == 2


def test_child_counts_are_direct_children_not_the_subtree(conn):
    """`child_count` and `type_count` measure different things on purpose."""
    roots = _by_id(tree.roots(conn))
    assert roots[ALPHA].child_count == 1        # Alpha Child only
    assert roots[ALPHA].type_count == 2         # but two types below in total


def test_the_breadcrumb_is_root_first_on_both_backends(conn):
    path = tree.path(conn, ALPHA_GRANDCHILD)
    assert [g.group_id for g in path] == [ALPHA, ALPHA_CHILD, ALPHA_GRANDCHILD]


def test_subtree_ids_reach_the_bottom(conn):
    assert sorted(tree.subtree_ids(conn, ALPHA)) == [ALPHA, ALPHA_CHILD,
                                                     ALPHA_GRANDCHILD]
    assert tree.subtree_ids(conn, GAMMA) == [GAMMA]


def test_an_unknown_group_is_empty_everywhere(conn):
    assert tree.path(conn, -1) == []
    assert tree.subtree_ids(conn, -1) == []
    assert tree.type_ids(conn, -1) == []
    assert tree.children(conn, -1) == []


def test_the_two_backends_return_the_same_answers(conn):
    """The parity claim itself, as one comparable value per function.

    Parameterised over both engines, so the assertion runs twice and the
    recorded expectation is identical — which is what "same on both" means when
    there is only one process to run it in.
    """
    snapshot = {
        "roots": [(g.group_id, g.name, g.child_count, g.type_count)
                  for g in tree.roots(conn)],
        "alpha_children": [(g.group_id, g.type_count)
                           for g in tree.children(conn, ALPHA)],
        "path": [g.group_id for g in tree.path(conn, ALPHA_GRANDCHILD)],
        "subtree": sorted(tree.subtree_ids(conn, ALPHA)),
        "types": sorted(tree.type_ids(conn, ALPHA)),
    }
    assert snapshot == {
        "roots": [(ALPHA, "Alpha", 1, 2), (BETA, "Beta", 1, 2),
                  (GAMMA, "Gamma", 0, 0)],
        "alpha_children": [(ALPHA_CHILD, 2)],
        "path": [ALPHA, ALPHA_CHILD, ALPHA_GRANDCHILD],
        "subtree": [ALPHA, ALPHA_CHILD, ALPHA_GRANDCHILD],
        "types": [100, 102],
    }
