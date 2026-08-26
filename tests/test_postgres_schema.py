"""The schema declaration actually builds on Postgres.

Everything else in the suite runs on SQLite, which is exactly the blind spot
that makes a port go wrong: SQLite accepts a great deal that Postgres refuses,
so "the tests pass" says nothing about whether the destination will have it.

These run against a real server when one is reachable and skip when it is not,
so they cost nothing in a normal run and catch the whole class the moment
someone brings one up:

    docker run -d --name eve-pg -e POSTGRES_PASSWORD=eve -e POSTGRES_USER=eve \\
        -e POSTGRES_DB=eve_retroindustry -p 5433:5432 postgres:17

Point `EVE_TEST_POSTGRES_URL` somewhere else to use a different one.
"""
from __future__ import annotations

import os

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

DEFAULT_URL = "postgresql+psycopg://eve:eve@localhost:5433/eve_retroindustry"
URL = os.environ.get("EVE_TEST_POSTGRES_URL", DEFAULT_URL)


def _reachable(url: str) -> bool:
    try:
        from sqlalchemy import create_engine, text
    except ImportError:                                  # pragma: no cover
        return False
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(URL), reason=f"no Postgres at {URL} — see this module's docstring")


@pytest.fixture(scope="module")
def pg():
    """A database migrated to head, in its own schema so it cannot collide."""
    from sqlalchemy import create_engine, text

    from app.db.migrate import upgrade_to_head

    engine = create_engine(URL)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS pytest_eve CASCADE"))
        conn.execute(text("CREATE SCHEMA pytest_eve"))
        conn.commit()
    engine.dispose()

    scoped = URL + ("&" if "?" in URL else "?") + "options=-csearch_path%3Dpytest_eve"
    upgrade_to_head(scoped)

    engine = create_engine(scoped)
    yield engine
    engine.dispose()


def _rows(engine, sql):
    from sqlalchemy import text
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def test_the_baseline_migration_runs_on_postgres(pg):
    """The declaration is SQLAlchemy Core precisely so this is possible.

    Had the schema stayed as SQL strings, this would mean maintaining a second
    hand-translated copy — the hedge the design doc rules out.
    """
    from app.db.schema import APP_TABLES

    names = {r[0] for r in _rows(
        pg, "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='pytest_eve'")}
    assert APP_TABLES <= names, f"never created: {sorted(APP_TABLES - names)}"


def test_only_our_own_ids_are_generated(pg):
    """A sequence on a CCP-assigned id would be a lie about who mints it.

    SQLAlchemy turns a lone integer primary key into SERIAL by default, which
    would have hung one off `characters.character_id`, `sde_types.type_id` and
    every other natural key. `autoincrement=False` in the declaration is what
    stops that, and this is where it is checked.
    """
    generated = {r[0] for r in _rows(
        pg, "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='pytest_eve' AND column_default LIKE 'nextval%'")}
    assert generated == {"margin_watchlist", "production_projects",
                         "project_jobs", "project_plans", "sync_events"}
    # `sync_events` mints its own ids on purpose: the id *is* the cursor a
    # consumer resumes from, so it has to be monotonic and locally assigned.
    # Its sibling `char_jobs_cache` is keyed on a CCP character_id and is
    # absent from this set, which is the distinction the test exists to keep.


def test_epoch_columns_survive_2038(pg):
    """Postgres INTEGER is four bytes; SQLite's is eight.

    A unix timestamp in a Postgres `integer` stops working in January 2038, and
    the columns these came from held one quite happily.
    """
    narrow = [f"{r[0]}.{r[1]}" for r in _rows(
        pg, "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='pytest_eve' AND data_type='integer' "
            "AND column_name IN ('cached_at','updated_at')")]
    assert not narrow, f"these overflow in 2038: {narrow}"


def test_composite_keys_reject_a_null_component(pg):
    """SQLite does not enforce NOT NULL on composite primary key columns.

    `pi_extractor_cache` was declared without it and accepted NULLs for years.
    Postgres does not, which is why the declaration tightened it — this is the
    check that the tightening was necessary rather than cosmetic.
    """
    from sqlalchemy import text

    with pg.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO pi_extractor_cache (char_id, planet_id, product_id) "
                "VALUES (:c, NULL, :p)"), {"c": 1, "p": 2})


def test_the_upsert_helper_runs_on_postgres(pg):
    """`ON CONFLICT (pk) DO UPDATE` is the whole reason the 38 statements moved.

    Worth executing rather than string-matching: the conflict target has to name
    a real unique constraint, and Postgres is the one that checks.
    """
    from sqlalchemy import text

    from app.db.schema import upsert

    # This test used to carry a local `?` -> `:p0, :p1` shim, because the helper
    # emitted positional placeholders "while 316 call sites pass tuples". Those
    # call sites reached zero in v0.9.74 and the helper emits named binds as of
    # v0.9.80, so the statement now goes straight into `text()` — which is the
    # thing this test is supposed to be demonstrating.
    full = upsert("station_rigs", ["location_id", "me_bonus_pct", "updated_at",
                                   "structure_type", "rig1_type_id"])
    partial = upsert("station_rigs", ["location_id", "me_bonus_pct", "updated_at"])

    with pg.connect() as conn:
        conn.execute(text(full), {"location_id": 60003760, "me_bonus_pct": 2.0,
                                  "updated_at": 0, "structure_type": "Raitaru",
                                  "rig1_type_id": 43920})
        conn.execute(text(partial), {"location_id": 60003760,
                                     "me_bonus_pct": 4.4, "updated_at": 1})
        conn.commit()
        row = conn.execute(text(
            "SELECT me_bonus_pct, structure_type, rig1_type_id FROM station_rigs"
        )).fetchone()

    assert tuple(row) == (4.4, "Raitaru", 43920), "unnamed columns were reset"
