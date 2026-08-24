"""The hand-written SQL has to survive the move to Postgres, and agree with the schema.

Step 4's Postgres port is not mostly a schema problem — `app/db/schema.py`
already emits Postgres DDL. It is a *query* problem: roughly 316 hand-written
statements that have only ever run against SQLite. These tests fence off the
constructs that would not survive, so the pile stops growing while the rest of
the port is done.

They are source scans rather than execution tests on purpose. There is no
Postgres in this suite to run against, and "this statement is portable" is a
claim about every call site, which only a call-site check can see.
"""
from __future__ import annotations

import pathlib
import ast
import re
import sqlite3

import pytest

from app.db.schema import apply_schema, apply_sde_schema, metadata, upsert

REPO = pathlib.Path(__file__).resolve().parents[1]
SOURCES = sorted(
    list((REPO / "app").rglob("*.py"))
    + [REPO / "import_sde.py", REPO / "main.py", REPO / "plan.py"]
)

# Scans run over raw source, not the AST: most of these statements are built
# from adjacent string literals, and the AST hands those over one fragment at a
# time, which splits statements down the middle.
SCHEMA_MODULE = "app/db/schema.py"


def _sources():
    for path in SOURCES:
        rel = path.relative_to(REPO).as_posix()
        yield rel, path.read_text(encoding="utf-8")


def _strip_concatenation(text: str) -> str:
    """Join `"... " "..."` fragments so a statement reads as one string."""
    return re.sub(r'["\']\s*["\']', " ", text)


def _line_of(src: str, index: int) -> int:
    return src[:index].count("\n") + 1


def _blank_docstrings(src: str) -> str:
    """Blank out docstring bodies, keeping every line number intact.

    A docstring explaining why `INSERT OR REPLACE` is gone is not a use of it.
    That false positive had been handled twice by hand — once with a "line
    starts with #" check, once by exempting the whole of `app/db/schema.py` —
    and the second of those hid every real offender in the file it exempted.
    This is the precise version: a docstring is never executable SQL, and
    nothing else is touched.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:            # not this scan's business
        return src
    lines = src.splitlines(keepends=True)
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        first = node.body[0]
        for i in range(first.lineno - 1, (first.end_lineno or first.lineno)):
            if i < len(lines):
                lines[i] = "\n"
    return "".join(lines)


# --- upserts ------------------------------------------------------------------

def test_no_insert_or_replace_survives():
    """`INSERT OR REPLACE` is SQLite-only, and it was also quietly destructive.

    It deletes the conflicting row and inserts a new one, so every column the
    statement does not name is reset. Eight call sites were doing that — see
    `app.db.schema.upsert` for what each of them lost. `ON CONFLICT ... DO
    UPDATE` runs on both dialects and writes only what it is given, so the
    portability fix and the data-loss fix are the same edit.
    """
    offenders = []
    for rel, src in _sources():
        # Docstrings are blanked rather than whole files skipped, so a module
        # that *documents* the pattern is still scanned for uses of it.
        src = _blank_docstrings(src)
        # `OR IGNORE` is the same family: SQLite-only, and `ON CONFLICT DO
        # NOTHING` says the same thing in both dialects.
        for m in re.finditer(r"INSERT\s+OR\s+(REPLACE|IGNORE)", src, re.I):
            # A comment explaining why it is gone is not a use of it.
            line = src.splitlines()[_line_of(src, m.start()) - 1].lstrip()
            if line.startswith("#"):
                continue
            offenders.append(f"{rel}:{_line_of(src, m.start())} (OR {m.group(1).upper()})")
    assert not offenders, (
        "SQLite-only INSERT forms: " + ", ".join(offenders)
        + ". Use app.db.schema.upsert(), or an explicit "
          "ON CONFLICT ... DO UPDATE / DO NOTHING clause."
    )


def test_every_upsert_conflicts_on_a_real_key():
    """`ON CONFLICT (cols)` must name a unique constraint or the statement errors.

    The conflict target is not checked until the statement runs, and several of
    these run only on paths that need a live ESI response — so a wrong target
    would sit there unnoticed until exactly the moment it mattered.
    """
    # The lookahead stops the match crossing a statement boundary. Without it a
    # plain INSERT with no conflict clause — `public_contract_items`, which has
    # no primary key and correctly does DELETE-then-INSERT — swallowed the
    # ON CONFLICT belonging to the next statement three lines down.
    stmt = re.compile(
        r"INSERT\s+INTO\s+(\w+)((?:(?!INSERT\s+INTO).)*?)ON\s+CONFLICT\s*\(([^)]*)\)",
        re.I | re.S)
    checked = 0
    for rel, src in _sources():
        for m in stmt.finditer(_strip_concatenation(src)):
            table, target = m.group(1), m.group(3)
            t = metadata.tables.get(table)
            assert t is not None, f"{rel}: {table} is not declared"
            named = {c.strip() for c in target.split(",") if c.strip()}
            keys = {c.name for c in t.primary_key.columns}
            uniques = [
                {c.name for c in con.columns}
                for con in t.constraints
                if con.__class__.__name__ == "UniqueConstraint"
            ]
            assert named == keys or named in uniques, (
                f"{rel}: {table} conflict target {sorted(named)} is neither its "
                f"primary key {sorted(keys)} nor a unique constraint")
            checked += 1
    assert checked > 30, f"only found {checked} upserts — the scan has stopped working"


def test_upsert_preserves_columns_it_was_not_asked_to_write(tmp_path):
    """The behaviour the eight sites needed and `OR REPLACE` denied them.

    Setting a station's ME bonus must not un-configure its rigs; caching a type
    name must not discard its packaged volume.
    """
    conn = sqlite3.connect(tmp_path / "t.db")
    apply_schema(conn)
    apply_sde_schema(conn)

    conn.execute(
        upsert("station_rigs", ["location_id", "me_bonus_pct", "updated_at",
                                "structure_type", "rig1_type_id"]),
        (60003760, 2.0, 0, "Raitaru", 43920))
    conn.execute(
        upsert("station_rigs", ["location_id", "me_bonus_pct", "updated_at"]),
        (60003760, 4.4, 1))

    row = conn.execute(
        "SELECT me_bonus_pct, structure_type, rig1_type_id FROM station_rigs").fetchone()
    conn.close()
    assert row == (4.4, "Raitaru", 43920), "the rig configuration was reset"


def test_upsert_refuses_a_partial_key():
    """A conflict target has to be the whole key, or it does not identify a row."""
    with pytest.raises(ValueError, match="whole key"):
        upsert("hub_price_cache", ["region_id", "sell_price"])


def test_upsert_reads_the_key_from_the_declaration():
    sql = upsert("sci_cache", ["solar_system_id", "activity", "cost_index", "cached_at"])
    assert "ON CONFLICT (solar_system_id, activity)" in sql
    assert "cost_index=excluded.cost_index" in sql
    assert "solar_system_id=excluded" not in sql, "key columns are not reassigned"


# --- statements and schema agree ---------------------------------------------

_INSERT = re.compile(r"INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)


def test_no_insert_omits_a_column_that_has_no_default():
    """Every NOT NULL column is either written or has something to fall back on.

    This exists because removing four SQLite-only `DEFAULT (strftime(...))`
    clauses from the schema broke exactly one insert — `solar_system_cache`,
    which relied on the default to fill `cached_at`. Existing databases still
    carried the old default, so nothing went red; only a database created by
    the new schema would have failed, on an ESI path with no coverage. That is
    the shape of bug this catches: statements and schema agreeing on the
    developer's machine and disagreeing on a fresh install.
    """
    offenders = []
    for rel, src in _sources():
        for m in _INSERT.finditer(_strip_concatenation(src)):
            table = m.group(1)
            t = metadata.tables.get(table)
            if t is None:
                continue
            named = {c.strip() for c in m.group(2).split(",") if c.strip()}
            if not named:
                continue
            for col in t.columns:
                if col.name in named or col.nullable:
                    continue
                if col.server_default is not None or col.default is not None:
                    continue
                if col is t.autoincrement_column:
                    continue                       # the database mints it
                offenders.append(
                    f"{rel}:{_line_of(src, m.start())} {table}.{col.name}")
    assert not offenders, (
        "these inserts omit a NOT NULL column with no default, so they fail on "
        "a database built from the current schema: " + ", ".join(sorted(set(offenders)))
    )


def test_the_security_status_cache_can_be_written_to_a_fresh_database(tmp_path):
    """The specific statement the scan above was written for.

    Worth its own test because the scan reasons about source text while this
    one actually runs the statement.
    """
    import time

    conn = sqlite3.connect(tmp_path / "t.db")
    apply_schema(conn)
    conn.execute(
        upsert("solar_system_cache", ["system_id", "security_status", "cached_at"]),
        (30000142, 0.9, int(time.time())))
    assert conn.execute("SELECT security_status FROM solar_system_cache").fetchone()[0] == 0.9
    conn.close()


# --- other SQLite-only constructs --------------------------------------------

def test_no_sqlite_only_functions_in_queries():
    """`strftime` and `datetime('now')` have no Postgres equivalent by that name.

    They are gone from the schema; this keeps them from coming back through a
    query instead.
    """
    # `.strftime(` is datetime's method and has nothing to do with SQL — four of
    # those, and the schema module names the function in a docstring. Only a
    # bare call is the SQLite one.
    call = re.compile(r"(?<![.\w])(strftime|julianday)\s*\(")
    offenders = []
    for rel, src in _sources():
        if rel == SCHEMA_MODULE:
            continue
        for m in call.finditer(src):
            offenders.append(f"{rel}:{_line_of(src, m.start())} {m.group(1)}")
    assert not offenders, "SQLite-only date functions: " + ", ".join(offenders)


def test_no_unaccounted_sqlite_pragmas():
    """The useful question is *which* pragma, not which file uses it.

    Opening a connection is legitimately dialect-specific — WAL and
    busy_timeout are what stopped "database is locked" when a character sync
    and a token refresh wrote at once, and Postgres has no equivalent question
    to ask. Introspection is different: `table_info` and `database_list` are
    real queries that each need an `information_schema` rewrite.

    So this pins the *set* of pragmas in use. A new one goes red, and whoever
    adds it has to say which of the two kinds it is.
    """
    connection_setup = {"busy_timeout", "journal_mode", "synchronous", "foreign_keys"}
    introspection = {"table_info", "database_list"}

    seen: dict[str, str] = {}
    for rel, src in _sources():
        if rel == SCHEMA_MODULE:
            continue                    # explains the old PRAGMA probes in prose
        for m in re.finditer(r"\bPRAGMA\s+(\w+)", src, re.I):
            seen.setdefault(m.group(1).lower(), f"{rel}:{_line_of(src, m.start())}")

    unexpected = {n: w for n, w in seen.items()
                  if n not in connection_setup | introspection}
    assert not unexpected, (
        "new SQLite-only pragmas, each needing a Postgres answer: "
        + ", ".join(f"{n} ({w})" for n, w in sorted(unexpected.items())))
    assert seen, "the scan found no pragmas at all, which cannot be right"


def test_the_scan_asks_sqlalchemy_which_column_the_database_fills():
    """It used to check `col.autoincrement is True`, which is a spelling.

    SQLAlchemy's default for a lone integer primary key is the string `'auto'`,
    resolved at DDL time — so a table declared the idiomatic way was reported
    as omitting its own id. Every table in the schema happened to spell it out,
    which is why the scan looked right. `Table.autoincrement_column` is the
    answer SQLAlchemy itself uses to emit the DDL.
    """
    from sqlalchemy import Column, Integer, MetaData, Table, Text

    probe = MetaData()
    spelled = Table("spelled", probe,
                    Column("id", Integer, primary_key=True, autoincrement=True))
    default = Table("default_", probe, Column("id", Integer, primary_key=True))
    natural = Table("natural", probe,
                    Column("id", Integer, primary_key=True, autoincrement=False),
                    Column("v", Text))

    assert spelled.autoincrement_column is spelled.c.id
    assert default.autoincrement_column is default.c.id, (
        "the default spelling is still a column the database fills"
    )
    assert natural.autoincrement_column is None, (
        "a natural key is not filled by the database, and an insert that omits "
        "it really is broken"
    )
