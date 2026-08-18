"""The schema is declared in exactly one place, and it describes the real database.

Step 4 (`docs/design-hosted-v2.md` §11) needs a baseline to migrate from, and
before this module there was not one: thirty-four DDL statements lived in
fourteen files, half applied at startup and half on first use, so two installs
of the same version could have different tables.

These tests hold that line. The rule they enforce is the one v0.9.29 taught
three times over — *a name that claims something about the world needs an
assertion about the world*. "The schema lives in app/db/schema.py" is a claim
about every other file, so it is checked by scanning every other file, not by
asking the schema module about itself.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sqlite3

import pytest

from app.db import schema as schema_mod
from app.db.schema import (
    APP_TABLES,
    SDE_TABLES,
    apply_schema,
    apply_sde_schema,
    metadata,
)

REPO = pathlib.Path(__file__).resolve().parents[1]

# The only file allowed to contain DDL. Everything else must go through it.
SCHEMA_MODULE = "app/db/schema.py"

# app/db/database.py declares two tables as SQLAlchemy models and creates them
# with Base.metadata.create_all — DDL, but expressed as models rather than SQL,
# so the source scan cannot see it. `test_the_orm_models_agree_with_the_schema`
# is what pins those two instead.
SOURCES = sorted(
    [p for p in (REPO / "app").rglob("*.py")]
    + [REPO / "import_sde.py", REPO / "main.py", REPO / "plan.py"]
)

# Anchored, not searched. A DDL statement begins with its keyword; prose that
# mentions one does not, and this module's own docstrings talk about CREATE
# TABLE at length. Leading whitespace is stripped first so the triple-quoted
# statements that start on the line after the quotes still match.
_DDL = re.compile(r"(CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX|VIEW)|ALTER\s+TABLE)\b", re.I)


def _string_literals(path: pathlib.Path):
    """Every string constant in a file, with its line number."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:                                     # pragma: no cover
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


# --- one source of truth -----------------------------------------------------

def test_no_ddl_lives_outside_the_schema_module():
    """A call-site check, because that is the only kind that can see this.

    A test that asked the schema module whether it contained the schema would
    pass no matter how many CREATE TABLEs the rest of the tree grew — exactly
    the failure that let eleven modules build their own httpx clients while
    `test_esi_client_sends_user_agent` stayed green.
    """
    offenders = []
    for path in SOURCES:
        rel = path.relative_to(REPO).as_posix()
        if rel == SCHEMA_MODULE:
            continue
        for lineno, value in _string_literals(path):
            if _DDL.match(value.strip()):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "DDL outside " + SCHEMA_MODULE + ": " + ", ".join(offenders) + ". "
        "Declare the table there instead; the migration baseline is generated "
        "from that module and cannot see a CREATE TABLE hiding in a helper."
    )


def test_the_ad_hoc_column_migrations_are_gone():
    """`PRAGMA table_info` + `ALTER TABLE ADD COLUMN` was the old migration system.

    Eight of them ran on every call to the ensure_* function that carried them,
    and being SQLite-only they were the single biggest obstacle to the port.
    The columns they added are baseline columns now; Alembic owns anything
    further.
    """
    offenders = []
    for path in SOURCES:
        rel = path.relative_to(REPO).as_posix()
        if rel == SCHEMA_MODULE:
            continue
        for lineno, value in _string_literals(path):
            if re.match(r"ALTER\s+TABLE", value.strip(), re.I):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, "ALTER TABLE outside the schema module: " + ", ".join(offenders)


# --- the declaration describes the database the app actually uses ------------

_QUERY_TABLE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z0-9_]*)", re.I)

# Names that follow FROM/JOIN/INTO/UPDATE without being tables: SQLite's own
# catalogue, the table-valued pragma functions, and `SET` — which is what
# `ON CONFLICT ... DO UPDATE SET` puts there.
_NOT_TABLES = {"sqlite_master", "sqlite_temp_master", "set"}

# A literal has to *begin* as SQL to be read as SQL. Merely containing the word
# "update" is not enough: "pi-cache update failed: {exc}" and "last-update
# timestamp" are both English, and both were read as table references by the
# looser check this replaced.
_STARTS_SQL = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.I)


def _tables_referenced_in_sql():
    """Table names the application's SQL mentions, with where they were found."""
    seen: dict[str, str] = {}
    for path in SOURCES:
        rel = path.relative_to(REPO).as_posix()
        for lineno, value in _string_literals(path):
            if not _STARTS_SQL.match(value):
                continue
            for name in _QUERY_TABLE.findall(value):
                low = name.lower()
                if low in _NOT_TABLES or low.startswith("pragma_"):
                    continue
                seen.setdefault(low, f"{rel}:{lineno}")
    return seen


def test_every_table_the_app_queries_is_declared():
    """The other half of "one source of truth": nothing is queried that nothing creates.

    A table that only a query knows about is a 500 waiting for the first
    person to visit that page on a database where some other code path has not
    run yet — which is the precise shape of the bug this consolidation exists
    to remove.
    """
    referenced = _tables_referenced_in_sql()

    # The filter above is deliberately strict, and a strict filter that matches
    # nothing would make this pass while checking nothing at all. Prove it still
    # sees the database before trusting what it says about it.
    declared = set(metadata.tables)
    assert len(set(referenced) & declared) > 25, (
        f"only recognised {len(set(referenced) & declared)} known tables in the "
        "app's SQL — the extraction has stopped working, not the schema"
    )

    undeclared = {
        name: where for name, where in referenced.items() if name not in declared
    }
    assert not undeclared, (
        "queried but never declared: "
        + ", ".join(f"{n} ({w})" for n, w in sorted(undeclared.items()))
    )


def test_a_fresh_database_gets_every_declared_table(tmp_path):
    """Both scopes, applied to an empty file, reproduce the whole schema."""
    conn = sqlite3.connect(tmp_path / "fresh.db")
    apply_schema(conn)
    apply_sde_schema(conn)
    built = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
    }
    conn.close()
    assert built == set(metadata.tables)


def test_the_two_scopes_do_not_overlap():
    """App tables are migrated and never dropped; SDE tables are replaced
    wholesale on every CCP build. A table in both would get both treatments."""
    assert APP_TABLES & SDE_TABLES == set()
    assert APP_TABLES | SDE_TABLES == set(metadata.tables)
    assert all(n.startswith("sde_") for n in SDE_TABLES)


def test_the_orm_models_agree_with_the_schema():
    """`app/db/database.py` still declares two tables as SQLAlchemy models.

    They are also declared in the schema module, so one file can describe the
    whole database — which means two definitions that must not drift. Whichever
    runs first wins and the other silently does nothing, so a mismatch would
    show up as a missing column at query time, not here, without this test.
    """
    from app.db.database import Base

    for name in ("type_cache", "blueprint_cache"):
        model = Base.metadata.tables[name]
        declared = metadata.tables[name]
        assert {c.name for c in model.columns} == {c.name for c in declared.columns}, name
        assert ({c.name for c in model.primary_key.columns}
                == {c.name for c in declared.primary_key.columns}), name


# --- the regression this consolidation surfaced ------------------------------

def test_the_blueprint_product_lookup_uses_its_index(tmp_path):
    """Not "the index exists" — "the query planner uses it".

    `product_type_id` is the third column of the primary key, so it gets no
    help from the implicit index; without `idx_bp_product` this is a scan.
    """
    conn = sqlite3.connect(tmp_path / "sde.db")
    apply_sde_schema(conn)
    plan = " ".join(str(r[-1]) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM sde_blueprint_products WHERE product_type_id=?",
        (34,)))
    conn.close()
    assert "idx_bp_product" in plan, plan
    assert "SCAN" not in plan, plan


# --- portability: the reason this is SQLAlchemy Core and not SQL strings -----

def test_the_declaration_emits_postgres_ddl_too():
    """One declaration, both dialects — the "decide once, not hedged" property.

    If this module were DDL strings, the port would mean hand-translating
    every statement and keeping two copies in step forever.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    for name in sorted(metadata.tables):
        sql = str(CreateTable(metadata.tables[name]).compile(dialect=postgresql.dialect()))
        assert "AUTOINCREMENT" not in sql.upper(), name      # SQLite-only keyword
        assert "strftime" not in sql, name                   # SQLite-only function


def test_generated_keys_are_serial_and_natural_keys_are_not():
    """An EVE character ID comes from CCP; a project ID is ours to mint.

    SQLAlchemy turns a lone integer primary key into SERIAL on Postgres by
    default, which would hang a sequence off every ID that CCP already
    assigned — harmless until something trusts it.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    def ddl(name):
        return str(CreateTable(metadata.tables[name]).compile(dialect=postgresql.dialect()))

    for name in ("production_projects", "project_plans", "project_jobs",
                 "margin_watchlist"):
        assert "SERIAL" in ddl(name), f"{name}.id should be generated"

    for name in ("characters", "sde_types", "market_price_cache", "app_owner"):
        assert "SERIAL" not in ddl(name), f"{name} has a natural key, not a generated one"


def test_epoch_columns_are_wide_enough_for_postgres():
    """Postgres INTEGER is four bytes and stops holding a unix timestamp in 2038.

    SQLite's INTEGER is eight, so the columns these came from were fine and
    would have ported to a column that is not.
    """
    from sqlalchemy import BigInteger

    for table in metadata.tables.values():
        for col in table.columns:
            if col.name in ("cached_at", "updated_at") and "INT" in str(col.type).upper():
                assert isinstance(col.type, BigInteger), f"{table.name}.{col.name}"


# --- the memo -----------------------------------------------------------------

def test_the_schema_memo_is_per_database_not_per_process(tmp_path):
    """The flag this replaced was a single process-wide boolean.

    One process, two databases — the second gets no schema at all if the memo
    is global, which is every test run and, after Step 5, every tenant.
    """
    schema_mod.forget_applied()
    first, second = tmp_path / "one.db", tmp_path / "two.db"
    for path in (first, second):
        conn = sqlite3.connect(path)
        schema_mod.ensure_schema(conn)
        conn.close()

    for path in (first, second):
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert APP_TABLES <= tables, f"{path.name} did not get the schema"


def test_forgetting_one_database_leaves_the_other_memoized(tmp_path):
    schema_mod.forget_applied()
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    for path in (a, b):
        conn = sqlite3.connect(path)
        schema_mod.ensure_schema(conn)
        conn.close()

    schema_mod.forget_applied(a)
    remembered = {entry[0] for entry in schema_mod._APPLIED}
    assert a not in remembered
    assert b in remembered


# --- the paths that rebuild the database ------------------------------------
#
# Three code paths can replace SDE tables, and each one used to lose something
# on the way through. They are tested together because the bug is the same
# every time: rebuilding a database from a file that describes only part of it.

def test_the_running_app_rebuilds_what_is_missing(app_module):
    """The claim is about `get_conn()`, so it is asked of `get_conn()` — and
    asked of a database that is actually missing something.

    The first version of this test checked a database the fixture had copied
    from `sde_base.db`, which already carries every SDE table and all six
    indexes. It passed whether or not `get_conn()` did anything at all. So this
    one takes them away first: four tables that were previously created lazily
    on whichever page happened to need them, and the indexes an SDE refresh
    drops. If the startup path stops guaranteeing them, this goes red.
    """
    declared_indexes = {
        ix.name for name in SDE_TABLES for ix in metadata.tables[name].indexes}
    lazy = {"public_contracts", "public_contract_items", "public_contract_meta",
            "route_jump_cache"}

    conn = app_module.get_conn()
    try:
        for name in lazy:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
        for name in declared_indexes:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.commit()
        remaining = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not (lazy & remaining), "the fixture did not actually lose anything"
    finally:
        conn.close()

    # Next connection, as the app would open it.
    schema_mod.forget_applied()
    conn = app_module.get_conn()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    finally:
        conn.close()

    assert APP_TABLES <= tables, f"never rebuilt: {sorted(APP_TABLES - tables)}"
    assert declared_indexes <= indexes, f"never rebuilt: {sorted(declared_indexes - indexes)}"


def test_no_code_path_replaces_the_live_database_file():
    """A call-site check, because the behavioural test above cannot see this.

    `test_downloading_the_sde_does_not_wipe_the_account` proves that
    `_refresh_sde_from_bundle` preserves user data. It says nothing about
    whether the download route still *calls* it — put `shutil.move(tmp, DB_ABS)`
    back in `setup_download` and that test stays green while the bug returns in
    full. The claim being made is about call sites, so it is checked at the
    call sites.

    Nothing is allowed to do it any more. The fresh-install copy was the last
    legitimate case, and it went when static data stopped being distributed as
    a file: the importer writes rows into whatever database it is pointed at,
    so no code path needs to install one.
    """
    import app.web.main as m

    allowed: set[str] = set()
    source = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed:
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in ("move", "copy", "copy2", "copyfile")
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "shutil"):
                offenders.append(f"{node.name}:{inner.lineno}")

    assert not offenders, (
        "these replace the database file wholesale rather than copying rows: "
        + ", ".join(offenders)
        + ". sde_base.db holds static data only, so moving it over eve_cache.db "
          "takes every character, token, price and project with it."
    )


def test_the_importer_indexes_what_it_builds():
    """The index guarantee, now that the bundled file is gone.

    Indexes used to arrive inside `sde_base.db` and get destroyed by the first
    refresh that dropped a table. Static data now arrives only by running
    `import_sde.py`, so the importer is the single place that has to get this
    right — and it does it by calling the same declaration everything else uses.
    """
    import import_sde

    assert import_sde.init_db.__module__ == "import_sde"
    declared = {ix.name for name in SDE_TABLES
                for ix in metadata.tables[name].indexes}
    assert declared, "the SDE declares no indexes at all"

    conn = sqlite3.connect(":memory:")
    try:
        import_sde.init_db(conn)
        built = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    assert declared <= built, f"importer skipped: {sorted(declared - built)}"
    assert SDE_TABLES <= tables


def test_static_data_is_no_longer_distributed_as_a_file():
    """`sde_base.db` is a test fixture now, not a runtime source.

    It stays in the repository so the suite is hermetic and needs no network.
    What changed is that nothing at runtime reads it: the app cannot copy a
    database into place, so what is in the database is whatever the importer
    last put there — current by construction rather than as current as the last
    release.
    """
    import app.web.main as m

    source = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    for gone in ("_bundled_sde_path", "_refresh_sde_from_bundle",
                 "SDE_DOWNLOAD_URL", "_SDE_TABLES_TO_REFRESH"):
        assert gone not in source, f"{gone} is still referenced in main.py"
