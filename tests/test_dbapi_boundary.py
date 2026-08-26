"""Every function that reaches the DBAPI connection guards the dialect first.

`app/db/schema.py` still speaks the raw driver: `ensure_schema()` takes a
`sqlite3.Connection` and memoises per database by asking `PRAGMA
database_list`. Two things follow, and both are accommodations rather than
design:

* eleven `ensure_*` shims reach through their SQLAlchemy connection to the
  driver underneath — `dbapi(conn)`, or the same thing spelled out as
  `conn.connection.driver_connection`;
* every one of them must open with `if conn.engine.dialect.name != "sqlite":
  return`, because `PRAGMA` is a **syntax error** on Postgres. There the tables
  come from Alembic, so the shim has nothing to do.

That invariant held by repetition and vigilance and nothing else. It had
already failed once: `ensure_project_tables` was missing the guard its five
siblings carried, and it was found in v0.9.76 only because the function was
being deleted for being callerless. A shim added tomorrow without the guard
ships a 500 on every Postgres request that touches its page, and the SQLite
suite stays green.

`app/db/conn.dbapi`'s own docstring says `grep -rn "dbapi("` is "the list of
boundaries still standing, and it should shrink to nothing." This file is what
makes that sentence true — the count is pinned below, so the list shrinks
deliberately and cannot grow by accident. Every comparable measure in this
codebase already has a call-site scan (raw statements, ESI traffic, DDL
placement, cache-only routes); this was the one that did not.

**Delete this file when `app/db/schema.py` stops speaking the DBAPI.** At that
point the shims take a SQLAlchemy connection like everything else, the guards
go with them, and there is no boundary left to pin.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app"

#: `dbapi()` *is* the accessor. Guarding it would be guarding the doorway
#: rather than the room, and its callers are what this file is about.
ACCESSOR = ("app/db/conn.py", "dbapi")

#: Every function known to reach the driver connection, and why it has to.
#: Each is a schema shim calling `ensure_db_schema`, which cannot yet take a
#: SQLAlchemy connection. Remove a name when its function stops reaching —
#: never to make this file pass.
BOUNDARIES = {
    ("app/auth/token_store.py", "ensure_characters_table"),
    ("app/market/prices.py", "ensure_price_table"),
    ("app/market/prices.py", "ensure_hist_etag_table"),
    ("app/web/app_defaults.py", "ensure_defaults_table"),
    ("app/web/bootstrap.py", "ensure_bootstrap_table"),
    ("app/web/contracts_helper.py", "ensure_public_contract_tables"),
    ("app/web/industry_helper.py", "ensure_industry_tables"),
    ("app/web/location_resolver.py", "ensure_location_name_table"),
    ("app/web/margins_helper.py", "ensure_margin_tables"),
    ("app/web/routers/assets.py", "ensure_route_jump_table"),
    ("app/web/security.py", "ensure_sessions_table"),
}


def _reaches_dbapi(node: ast.AST) -> int | None:
    """Line where `node` first reaches the driver connection, or None.

    Two spellings, because both are in the tree: the `dbapi()` helper and the
    `conn.connection.driver_connection` attribute chain it wraps. A scan that
    knew only the helper would report a clean tree while six sites bypassed it
    — the same shape as `test_esi_client_sends_user_agent` asking the wrapper
    about itself while eleven callers went around it.
    """
    lines = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "dbapi":
            lines.append(n.lineno)
        elif isinstance(n, ast.Attribute) and n.attr == "driver_connection":
            lines.append(n.lineno)
    return min(lines) if lines else None


def _guard_line(node: ast.AST) -> int | None:
    """Line of `if conn.engine.dialect.name != "sqlite": return`, or None.

    Matched structurally rather than by string, so a docstring that mentions
    the dialect cannot stand in for the check — every one of these functions
    has such a docstring.
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.If) or not isinstance(n.test, ast.Compare):
            continue
        test = n.test
        if not (isinstance(test.left, ast.Attribute) and test.left.attr == "name"):
            continue
        if not (isinstance(test.left.value, ast.Attribute)
                and test.left.value.attr == "dialect"):
            continue
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.NotEq):
            continue
        rhs = test.comparators[0]
        if not (isinstance(rhs, ast.Constant) and rhs.value == "sqlite"):
            continue
        if len(n.body) == 1 and isinstance(n.body[0], ast.Return):
            return n.lineno
    return None


def _functions():
    """(relative path, function node) for every function defined under app/."""
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield rel, node


def _scan():
    """{(path, name): (reach_line, guard_line_or_None)} for every boundary."""
    found = {}
    for rel, fn in _functions():
        reach = _reaches_dbapi(fn)
        if reach is None or (rel, fn.name) == ACCESSOR:
            continue
        found[(rel, fn.name)] = (reach, _guard_line(fn))
    return found


# ── the invariant ────────────────────────────────────────────────────────────

def test_every_dbapi_boundary_guards_the_dialect():
    """The one that would have caught `ensure_project_tables`."""
    unguarded = sorted(k for k, (_, guard) in _scan().items() if guard is None)

    assert not unguarded, (
        "these reach the driver connection with no dialect guard, so they run "
        "PRAGMA on Postgres and raise:\n  "
        + "\n  ".join(f"{f}::{n}()" for f, n in unguarded)
    )


def test_the_guard_comes_before_the_reach():
    """A guard below the call is not a guard. Structurally easy to write —
    the shim is three lines and the order is the whole of it."""
    late = sorted(k for k, (reach, guard) in _scan().items()
                  if guard is not None and guard > reach)

    assert not late, f"guard sits after the DBAPI call in: {late}"


# ── the inventory ────────────────────────────────────────────────────────────

def test_the_boundary_list_is_exactly_what_is_in_the_tree():
    """So the count shrinks deliberately and cannot grow by accident.

    `dbapi()`'s docstring calls this list "the boundaries still standing" and
    says it should reach nothing. Pinning it is what turns that from a wish
    into something a commit has to argue with.
    """
    found = set(_scan())

    assert found == BOUNDARIES, (
        f"new boundaries: {sorted(found - BOUNDARIES)}\n"
        f"gone (delete from BOUNDARIES): {sorted(BOUNDARIES - found)}"
    )


# ── the control ──────────────────────────────────────────────────────────────

def test_the_scan_can_still_say_no():
    """A scan whose healthy answer is "nothing wrong" looks exactly like a scan
    that has stopped reading. Both spellings of the reach are checked, because
    a checker that knew only `dbapi()` would pass every one of the six sites
    that use the attribute chain instead."""
    guarded, bare_helper, bare_attr = (ast.parse(s).body[0] for s in [
        'def f(conn):\n'
        '    if conn.engine.dialect.name != "sqlite":\n'
        '        return\n'
        '    ensure_db_schema(dbapi(conn))\n',

        'def f(conn):\n'
        '    ensure_db_schema(dbapi(conn))\n',

        'def f(conn):\n'
        '    ensure_db_schema(conn.connection.driver_connection)\n',
    ])

    assert _reaches_dbapi(guarded) and _guard_line(guarded)
    assert _reaches_dbapi(bare_helper) and _guard_line(bare_helper) is None
    assert _reaches_dbapi(bare_attr) and _guard_line(bare_attr) is None


def test_a_docstring_mentioning_the_dialect_is_not_a_guard():
    """Every one of these functions documents the dialect check in prose. A
    string-matching version of `_guard_line` passed on the prose alone — which
    is the `INSERT OR REPLACE` docstring false-positive in a second costume."""
    prose_only = ast.parse(
        'def f(conn):\n'
        '    """Returns early when conn.engine.dialect.name != \\"sqlite\\"."""\n'
        '    ensure_db_schema(dbapi(conn))\n'
    ).body[0]

    assert _guard_line(prose_only) is None


def test_the_scan_reads_a_real_tree():
    """Positive control on the walk itself: a wrong root, or a glob that stops
    matching, gives an empty result that every assertion above welcomes."""
    assert len(list(_functions())) > 200
    assert len(_scan()) == len(BOUNDARIES) >= 11
