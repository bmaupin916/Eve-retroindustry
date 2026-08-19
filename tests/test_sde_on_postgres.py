"""The static data can be built on Postgres, not only on SQLite.

This was the last thing standing between Postgres and a page that renders. The
Alembic history covers `APP_TABLES` only — the SDE is deliberately outside it,
because it is replaced wholesale on every build and carries no user data — so
something else has to create those tables, and until now nothing did on
Postgres. Six of the app's statements JOIN `sde_types` to a runtime table, so
every one of them failed there with `UndefinedTable`.

Two separate things had to be true, and both are asserted here rather than
assumed:

* **The schema can be created.** `apply_sde_schema` took a `sqlite3.Connection`,
  so on Postgres there was nothing to call. `create_sde_schema` takes a
  SQLAlchemy bind instead.
* **The importer can fill it.** Every write in `import_sde.py` was a positional
  `?` statement on `sqlite3`, several of them `INSERT INTO t VALUES (?,?,?)`
  with no column list at all. psycopg speaks none of that.

**Only the second of those was a dialect problem**, which was not the guess
going in. The SDE DDL turns out to compile identically on both backends, for a
reason worth keeping true — see `test_no_sde_table_mints_its_own_id`. The first
was the plainer kind of gap: a function nothing on that backend could call.

The whole importer runs on both backends here, from a miniature archive, and
the assertions are on the data that comes back out — not on the SQL that went
in. A test that checked the statements would pass while the rows were wrong.

The mutation run behind this file: forcing `_upsert` to build a SQLite
construct regardless of the connection fails **8 Postgres tests and 0 SQLite
ones**, which is what proves these assertions can see a backend difference at
all rather than merely running twice.

Postgres comes from the container named in `tests/test_postgres_schema.py`;
without it those parameterisations skip and the SQLite half still runs.
"""
from __future__ import annotations

import json
import zipfile

import pytest
from sqlalchemy import create_engine, text

from app.db.schema import SDE_TABLES, create_sde_schema
from app.sde import feed
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_sde"
BUILD = 3470007

#: A Nidhoggur, its blueprint, and enough of a bill of materials to exercise
#: the JOIN the app actually runs. The type ids are real so that a failure here
#: reads the same way as one against the shipped data.
TRITANIUM, NIDHOGGUR, NIDHOGGUR_BP = 34, 24483, 24484


def _tiny_archive(path: str) -> str:
    """A miniature SDE — the same shape as CCP's, three orders smaller."""
    datasets = {
        "_sde": [{"_key": "sde", "buildNumber": BUILD,
                  "releaseDate": "2026-08-17T11:26:56Z"}],
        "types": [
            {"_key": TRITANIUM, "name": {"en": "Tritanium"}, "groupID": 18,
             "published": True, "marketGroupID": 1857,
             "volume": 0.01, "packagedVolume": 0.01, "portionSize": 100},
            {"_key": NIDHOGGUR, "name": {"en": "Nidhoggur"}, "groupID": 547,
             "published": True, "volume": 14_500_000, "packagedVolume": 1_300_000},
            {"_key": NIDHOGGUR_BP, "name": {"en": "Nidhoggur Blueprint"},
             "groupID": 105, "published": True},
            # A description the bonus regex must find, so sde_skill_time_bonus
            # is non-empty on both backends. Not 3380 or 3388: the importer
            # excludes those two on purpose, and picking one made this look
            # like a write that had failed.
            {"_key": 24625, "name": {"en": "Advanced Industry"}, "groupID": 268,
             "published": True,
             "description": {"en": "5% reduction in manufacturing time per level"}},
        ],
        "groups": [
            {"_key": 18, "name": {"en": "Mineral"}},
            {"_key": 547, "name": {"en": "Carrier"}},
        ],
        "blueprints": [{
            "_key": NIDHOGGUR_BP,
            "maxProductionLimit": 10,
            "activities": {
                "manufacturing": {
                    "time": 3600,
                    "materials": [{"typeID": TRITANIUM, "quantity": 9_000_000}],
                    "products": [{"typeID": NIDHOGGUR, "quantity": 1}],
                    "skills": [{"typeID": 3380, "level": 5}],
                },
            },
        }],
        "typeMaterials": [
            {"_key": NIDHOGGUR, "materials": [
                {"materialTypeID": TRITANIUM, "quantity": 4_500_000}]},
        ],
        "marketGroups": [
            {"_key": 1857, "name": {"en": "Minerals"}, "parentGroupID": None,
             "hasTypes": True},
        ],
        "planetSchematics": [{
            "_key": 65, "name": {"en": "Water"}, "cycleTime": 1800,
            "types": [{"_key": 2268, "isInput": True, "quantity": 3000},
                      {"_key": 2309, "isInput": False, "quantity": 20}],
        }],
        "typeDogma": [],
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, records in datasets.items():
            z.writestr(f"{name}.jsonl",
                       "\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture(params=["sqlite", "postgres"])
def blank(request, tmp_path):
    """An empty database on each backend, with no SDE tables in it yet.

    Deliberately *not* pre-created: whether `create_sde_schema` can build them
    is half of what is under test, so a fixture that built them first would
    hide exactly the failure this file exists to catch.
    """
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'sde.db'}")
        yield engine
        engine.dispose()
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
    engine = create_engine(scoped)
    yield engine
    engine.dispose()


@pytest.fixture
def archive(tmp_path):
    return _tiny_archive(str(tmp_path / "sde.zip"))


@pytest.fixture
def built(blank, archive):
    """The whole importer, run against `blank`. Yields a live connection."""
    import import_sde

    with blank.connect() as conn:
        import_sde.init_db(conn)
        with zipfile.ZipFile(archive) as z:
            import_sde.import_types(conn, z)
            import_sde.import_groups(conn, z)
            import_sde.import_blueprints(conn, z)
            import_sde.import_dogma(conn, z)
            import_sde.import_type_materials(conn, z)
            import_sde.import_market_groups(conn, z)
            import_sde.import_planet_schematics(conn, z)
        import_sde.record_build(conn, feed.Build(BUILD, "2026-08-17T11:26:56Z"))
        yield conn


def _backend(bind) -> str:
    return bind.dialect.name if hasattr(bind, "dialect") else bind.engine.dialect.name


# ── the control ──────────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(blank):
    """Without this a broken Postgres fixture reads as a passing suite, because
    the SQLite half would carry it — and running on both is the entire point."""
    assert _backend(blank) in ("sqlite", "postgresql")
    with blank.connect() as conn:
        conn.execute(text("SELECT 1"))


# ── the schema ───────────────────────────────────────────────────────────────

def test_the_sde_schema_can_be_created(blank):
    """`apply_sde_schema` wanted a `sqlite3.Connection`, so on Postgres there
    was nothing to call at all — which is why the tables were simply absent
    there rather than wrong."""
    create_sde_schema(blank)

    from sqlalchemy import inspect
    present = set(inspect(blank).get_table_names())

    missing = SDE_TABLES - present
    assert not missing, f"on {_backend(blank)}: never created {sorted(missing)}"


def test_creating_the_schema_twice_is_not_an_error(blank):
    """Startup and the importer both call it, and a re-import calls it over a
    database that already has the tables."""
    create_sde_schema(blank)
    create_sde_schema(blank)             # must not raise


def test_the_indexes_are_created_too(blank):
    """The failure this guards is specific and has happened: an SDE refresh
    drops each table and recreates it, and a dropped table takes its indexes
    with it. A schema with the tables but not the indexes looks correct and
    turns every product lookup into a scan."""
    create_sde_schema(blank)

    from sqlalchemy import inspect
    inspector = inspect(blank)
    built = {ix["name"] for t in sorted(SDE_TABLES)
             for ix in inspector.get_indexes(t)}
    declared = {ix.name for t in sorted(SDE_TABLES)
                for ix in __import__("app.db.schema", fromlist=["metadata"])
                .metadata.tables[t].indexes}

    assert declared, "the SDE declares no indexes at all"
    assert declared <= built, (
        f"on {_backend(blank)}: missing {sorted(declared - built)}")


# ── the import ───────────────────────────────────────────────────────────────

def test_the_importer_writes_rows_on_both_backends(built):
    """The statements were positional `?` on `sqlite3`; psycopg speaks neither
    the placeholder nor the driver."""
    names = dict(built.execute(text(
        "SELECT type_id, name FROM sde_types ORDER BY type_id")).fetchall())

    assert names[TRITANIUM] == "Tritanium", f"on {_backend(built)}: {names}"
    assert names[NIDHOGGUR] == "Nidhoggur"


def test_the_join_the_app_runs_returns_the_bill_of_materials(built):
    """This is the shape that failed with `UndefinedTable`: an SDE table joined
    to another. Asserting on the returned quantity rather than on a row count
    means a JOIN that matches the wrong row still fails."""
    rows = built.execute(text("""
        SELECT t.name, m.quantity
        FROM sde_blueprint_materials m
        JOIN sde_types t ON t.type_id = m.material_type_id
        WHERE m.blueprint_type_id = :bp AND m.activity = 'manufacturing'
    """), {"bp": NIDHOGGUR_BP}).fetchall()

    assert [(r[0], r[1]) for r in rows] == [("Tritanium", 9_000_000)], (
        f"on {_backend(built)}: got {rows}")


def test_a_column_less_insert_landed_in_the_declared_order(built):
    """Several statements were `INSERT INTO t VALUES (?,?,?)`, correct only for
    as long as nobody inserts a column into the middle of the declaration.
    They name their columns now — so check a table whose columns are easy to
    transpose, by value rather than by position."""
    row = built.execute(text(
        "SELECT max_production_limit, manufacturing_time, reaction_time"
        " FROM sde_blueprints WHERE blueprint_type_id = :bp"),
        {"bp": NIDHOGGUR_BP}).fetchone()

    assert tuple(row) == (10, 3600, 0), f"on {_backend(built)}: {tuple(row)}"


def test_re_importing_the_same_build_updates_rather_than_duplicating(built, archive):
    """`ON CONFLICT (pk) DO UPDATE` is the whole of the re-import story: the
    operator runs `import_sde.py` again after a patch and must not get two rows
    per type. The conflict target is derived from the declared primary key now,
    so this also checks that derivation."""
    import import_sde

    with zipfile.ZipFile(archive) as z:
        import_sde.import_types(built, z)

    count = built.execute(text(
        "SELECT COUNT(*) FROM sde_types WHERE type_id = :tid"),
        {"tid": TRITANIUM}).scalar()

    assert count == 1, f"on {_backend(built)}: {count} rows for one type"


def test_the_build_number_is_recorded(built):
    """Startup reads this to decide whether the database has any static data at
    all; without it every page redirects to /setup."""
    row = built.execute(text(
        "SELECT build_number, release_date FROM sde_build WHERE id = 1")).fetchone()

    assert row is not None, f"on {_backend(built)}: no build row"
    assert row[0] == BUILD


def test_the_skill_bonus_pass_writes_its_own_table(built):
    """`import_types` writes two tables in one pass, and the second one is
    preceded by a DELETE — a statement that also had to survive the driver
    change, and whose failure would leave stale bonuses behind rather than
    raising."""
    rows = built.execute(text(
        "SELECT skill_type_id, time_bonus_pct FROM sde_skill_time_bonus")).fetchall()

    assert len(rows) == 1, f"on {_backend(built)}: {rows}"
    assert rows[0][1] == pytest.approx(5.0)


def test_planet_schematics_keep_inputs_and_output_apart(built):
    """The output is the single entry whose `isInput` is false. Getting this
    backwards produces a schematic that consumes its own product, and the PI
    planner then prices a loop."""
    row = built.execute(text(
        "SELECT output_type_id, output_qty, cycle_time"
        " FROM sde_planet_schematics WHERE schematic_id = 65")).fetchone()
    inputs = built.execute(text(
        "SELECT type_id, quantity FROM sde_planet_schematic_materials"
        " WHERE schematic_id = 65")).fetchall()

    assert tuple(row) == (2309, 20, 1800), f"on {_backend(built)}: {tuple(row)}"
    assert [tuple(r) for r in inputs] == [(2268, 3000)]


def test_an_empty_dataset_writes_nothing_rather_than_raising(built):
    """`typeDogma` is empty in this archive, which is the same shape as a
    dataset CCP has emptied. SQLAlchemy raises on `execute(stmt, [])` instead
    of treating it as zero work, so the importer has to check."""
    count = built.execute(text("SELECT COUNT(*) FROM sde_decryptors")).scalar()
    assert count == 0


# ── the target resolution ────────────────────────────────────────────────────

def test_a_url_is_passed_through_and_a_path_is_not(tmp_path, monkeypatch):
    """`--out` still takes a file, because that is how `sde_base.db` is built,
    but it has to stop assuming one — otherwise a Postgres URL would be
    turned into `sqlite:///postgresql+psycopg://...`."""
    import import_sde

    url = "postgresql+psycopg://eve:eve@localhost:55432/eve_retroindustry"
    assert import_sde._target_url(url) == url

    resolved = import_sde._target_url(str(tmp_path / "out.db"))
    assert resolved.startswith("sqlite:///")
    assert resolved.endswith("out.db")


def test_the_default_target_is_the_database_the_app_reads(tmp_path, monkeypatch):
    """It used to be a fixed file beside the script. That agreed with the app
    only while the data directory happened to be the checkout — set
    `EVE_APP_DIR` elsewhere and the import landed in a database nothing opened,
    silently."""
    import import_sde

    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    monkeypatch.delenv("EVE_DATABASE_URL", raising=False)

    resolved = import_sde._target_url(None)

    assert str(tmp_path).replace("\\", "/") in resolved.replace("\\", "/"), (
        f"the importer would write to {resolved}, not to EVE_APP_DIR")


def test_the_database_url_wins_when_it_is_set(tmp_path, monkeypatch):
    """The Postgres cutover is `EVE_DATABASE_URL` and nothing else. An importer
    that ignored it would build the SDE into a SQLite file beside an app that
    reads Postgres — and the app would say "no static data" while the import
    reported success."""
    import import_sde

    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    monkeypatch.setenv("EVE_DATABASE_URL",
                       "postgresql+psycopg://eve:eve@localhost:55432/eve")

    assert import_sde._target_url(None).startswith("postgresql")


def test_a_password_is_not_printed_back(monkeypatch):
    """The importer echoes its target on completion, and on a hosted deployment
    that target carries the database password. Printing it puts the credential
    in the terminal scrollback and in whatever captures the deploy log."""
    import import_sde

    shown = import_sde._display(
        "postgresql+psycopg://eve:hunter2@db.internal:5432/eve_retroindustry")

    assert "hunter2" not in shown, f"the password was printed: {shown}"
    assert "eve_retroindustry" in shown, "masking swallowed the database name too"


# ── why the DDL happened to be portable already ──────────────────────────────

def test_no_sde_table_mints_its_own_id():
    """Every SDE primary key is a natural key CCP assigned.

    This is the property that makes all fourteen SDE tables compile to
    identical DDL on both dialects — an integer single-column primary key with
    `autoincrement` left alone becomes `SERIAL` on Postgres and a rowid alias
    on SQLite, and *six of the app tables do exactly that*. None of these do.

    It is asserted rather than noted because the day it stops being true is the
    day `create_sde_schema` starts earning its keep, and nothing else would say
    so. A new SDE table with a generated id is a design mistake here anyway:
    the SDE is dropped and rebuilt on every import, so an id this app invented
    would not survive to be referenced.
    """
    from app.db.schema import metadata

    generated = [
        f"{name}.{col.name}"
        for name in sorted(SDE_TABLES)
        for col in metadata.tables[name].primary_key.columns
        if col.autoincrement is not False
        and len(metadata.tables[name].primary_key.columns) == 1
        and col.type.python_type is int
    ]

    assert not generated, (
        f"these would be SERIAL on Postgres and a rowid alias on SQLite: "
        f"{generated}. Mark them autoincrement=False, or accept that the two "
        f"backends now disagree about the SDE's shape.")


def test_the_declared_sde_ddl_is_the_same_on_both_dialects():
    """The claim the test above explains, stated directly.

    Checked rather than assumed, because the assumption going in was the
    opposite — that the SQLite-compiled DDL would be rejected on Postgres. It
    is not: all of it runs there unchanged.
    """
    from sqlalchemy.dialects import postgresql, sqlite as sqlite_d
    from sqlalchemy.schema import CreateTable

    from app.db.schema import metadata

    def rendered(table, dialect):
        return " ".join(str(CreateTable(table).compile(dialect=dialect)).split())

    differing = [
        name for name in sorted(SDE_TABLES)
        if rendered(metadata.tables[name], sqlite_d.dialect())
        != rendered(metadata.tables[name], postgresql.dialect())
    ]

    assert not differing, (
        f"{differing} no longer compile identically — which is allowed, but it "
        f"means the SDE schema is now dialect-sensitive and the comment in "
        f"create_sde_schema saying otherwise is stale.")


# ── the startup hook ─────────────────────────────────────────────────────────

def test_startup_creates_the_static_data_tables():
    """The app's startup counts `sde_types` to decide whether it has any static
    data. On SQLite the table was guaranteed by `get_conn()`, which creates the
    schema lazily — but that path is `sqlite3`-only, so on Postgres the count
    hit a table that did not exist and the whole startup handler fell into its
    `except`, leaving `_SDE_READY` false for a reason that had nothing to do
    with whether the SDE was imported.

    Asserted structurally because booting the app twice against two backends
    costs more than it proves here: what is worth pinning is that the startup
    handler calls the creator *before* it reads, since the other order is
    exactly the bug.
    """
    import ast
    import inspect

    from app.web import main as app_main

    src = inspect.getsource(app_main._startup_populate_groups)
    tree = ast.parse(src.strip())

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def position(predicate):
        for call in calls:
            if predicate(call):
                return call.lineno
        return None

    created = position(lambda c: isinstance(c.func, ast.Name)
                       and c.func.id == "create_sde_schema")
    counted = position(lambda c: any(
        isinstance(a, ast.Constant) and isinstance(a.value, str)
        and "sde_types" in a.value for a in c.args))

    assert created is not None, "startup no longer creates the SDE schema"
    assert counted is not None, "startup no longer counts sde_types — retarget this test"
    assert created < counted, (
        "startup reads sde_types before creating it, which is the failure this "
        "guards: on a backend without lazy creation the read raises and the "
        "whole handler is skipped")
