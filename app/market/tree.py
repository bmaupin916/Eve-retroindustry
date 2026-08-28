"""The in-game market group tree, as a read model.

`sde_market_groups` has been imported and populated since the SDE subset was
built — 2,106 groups, 19 roots, no orphans in either direction — and until this
module nothing in `app/` read a single row of it. §9.4 of the design doc still
describes the table as missing, which is why the Prices page is a flat list of
19,667 types: not because the tree was unavailable, but because nobody had
walked it.

**The tree is the aggregation axis, not navigation.** A group node is the unit a
KPI is computed over — "Battleships: median margin, ISK/day traded, 3 of 47
worth building" — which is what turns the page from lookup into scanning. So the
functions here return counts and id sets, and deliberately not rendering data.

### `has_types` is not trustworthy, and the counts here are computed

The SDE marks 1,665 groups `hasTypes`. Measured against the types themselves,
**54 of those contain no published type**, and **2 groups that claim none do
contain published types**. Wrong in both directions, so nothing here branches on
the flag; it is carried through for reference and every count comes from
`sde_types`. A browser that trusted it would offer 54 empty branches and hide
two real ones.

### Portability

Recursive CTEs, which SQLite and PostgreSQL both spell `WITH RECURSIVE`. Counts
come back in a second statement joined in Python rather than as a correlated
subquery over the CTE — same result, and it keeps both statements plain enough
to read on either backend.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text


@dataclass(frozen=True)
class Group:
    """One node of the market tree.

    `type_count` is the number of published types in this group **and every
    group beneath it**, so a parent's count is the size of the market it
    represents rather than the handful of types filed directly against it.
    """
    group_id: int
    parent_id: int | None
    name: str
    has_types: bool          # the SDE's claim; see the module docstring
    child_count: int
    type_count: int


# Every group in the subtree rooted at each of a parent's children, labelled
# with which child it descends from. Used to total types per child in one pass.
_SUBTREE_OF_CHILDREN = """
WITH RECURSIVE sub(root_id, gid) AS (
    SELECT market_group_id, market_group_id
      FROM sde_market_groups
     WHERE {parent_clause}
    UNION ALL
    SELECT s.root_id, g.market_group_id
      FROM sde_market_groups g
      JOIN sub s ON g.parent_group_id = s.gid
)
SELECT s.root_id, COUNT(t.type_id)
  FROM sub s
  LEFT JOIN sde_types t
         ON t.market_group_id = s.gid AND t.published = 1
 GROUP BY s.root_id
"""

_CHILD_ROWS = """
SELECT g.market_group_id, g.parent_group_id, g.name, g.has_types,
       (SELECT COUNT(*) FROM sde_market_groups c
         WHERE c.parent_group_id = g.market_group_id)
  FROM sde_market_groups g
 WHERE {parent_clause}
 ORDER BY g.name
"""


def parent_clause(parent_id: int | None, prefix: str = "") -> tuple[str, dict]:
    """`parent_group_id` compared against a value, or against NULL for roots.

    `= :pid` never matches NULL, so the 19 roots need `IS NULL` and there is no
    single statement that serves both. Two spellings, one place. `prefix` is the
    table alias the clause is being dropped into ("g." in the listing query,
    empty in the CTE where the table is unaliased) — passed in rather than
    patched in afterwards, because a string replace over SQL is the kind of
    thing that keeps working right up until an alias is renamed.
    """
    col = f"{prefix}parent_group_id"
    if parent_id is None:
        return f"{col} IS NULL", {}
    return f"{col} = :pid", {"pid": parent_id}


def children(conn, parent_id: int | None = None) -> list[Group]:
    """Direct children of `parent_id`, or the roots when it is None.

    Alphabetical, because the SDE carries no display order for market groups —
    `iconID` is the only other presentational field and it does not imply one.
    """
    listing, params = parent_clause(parent_id, "g.")
    rows = conn.execute(
        text(_CHILD_ROWS.format(parent_clause=listing)), params,
    ).fetchall()
    if not rows:
        return []
    cte, _ = parent_clause(parent_id)
    counts = dict(conn.execute(
        text(_SUBTREE_OF_CHILDREN.format(parent_clause=cte)), params,
    ).fetchall())
    return [
        Group(group_id=r[0], parent_id=r[1], name=r[2], has_types=bool(r[3]),
              child_count=r[4], type_count=int(counts.get(r[0], 0)))
        for r in rows
    ]


def roots(conn) -> list[Group]:
    """The top level of the market window — 19 groups at the current build."""
    return children(conn, None)


def path(conn, group_id: int) -> list[Group]:
    """Breadcrumb from the root down to `group_id` inclusive.

    Empty when the id does not exist, which is the answer a caller wants for a
    stale bookmark: no path rather than a one-element path to nothing.
    """
    rows = conn.execute(
        text("""
        WITH RECURSIVE up(market_group_id, parent_group_id, name, has_types, depth) AS (
            SELECT market_group_id, parent_group_id, name, has_types, 0
              FROM sde_market_groups WHERE market_group_id = :gid
            UNION ALL
            SELECT g.market_group_id, g.parent_group_id, g.name, g.has_types, u.depth + 1
              FROM sde_market_groups g
              JOIN up u ON g.market_group_id = u.parent_group_id
        )
        SELECT market_group_id, parent_group_id, name, has_types FROM up
         ORDER BY depth DESC
        """),
        {"gid": group_id},
    ).fetchall()
    if not rows:
        return []
    # A breadcrumb is navigation, not a market: the per-node counts a listing
    # needs are not worth four extra recursive queries here.
    return [Group(group_id=r[0], parent_id=r[1], name=r[2], has_types=bool(r[3]),
                  child_count=0, type_count=0) for r in rows]


def subtree_ids(conn, group_id: int) -> list[int]:
    """`group_id` and every group beneath it. Empty if the id does not exist."""
    rows = conn.execute(
        text("""
        WITH RECURSIVE sub(gid) AS (
            SELECT market_group_id FROM sde_market_groups WHERE market_group_id = :gid
            UNION ALL
            SELECT g.market_group_id FROM sde_market_groups g
              JOIN sub s ON g.parent_group_id = s.gid
        )
        SELECT gid FROM sub
        """),
        {"gid": group_id},
    ).fetchall()
    return [r[0] for r in rows]


def type_ids(conn, group_id: int) -> list[int]:
    """Published type ids anywhere in the subtree rooted at `group_id`.

    This is the set every group-level KPI aggregates over, so it filters on
    `published` for the same reason the refresh list does: an unpublished type
    has no market and would drag every average it entered.
    """
    rows = conn.execute(
        text("""
        WITH RECURSIVE sub(gid) AS (
            SELECT market_group_id FROM sde_market_groups WHERE market_group_id = :gid
            UNION ALL
            SELECT g.market_group_id FROM sde_market_groups g
              JOIN sub s ON g.parent_group_id = s.gid
        )
        SELECT t.type_id FROM sde_types t
          JOIN sub s ON t.market_group_id = s.gid
         WHERE t.published = 1
        """),
        {"gid": group_id},
    ).fetchall()
    return [r[0] for r in rows]
