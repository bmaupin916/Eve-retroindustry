"""Migrations, and the ways two sources of truth quietly disagree.

The schema is declared in `app/db/schema.py` and *also* described by the
migration history. Both are needed — the declaration is what emits DDL for a
fresh database and what Alembic diffs against; the history is what carries an
existing database forward — but they can drift, and drift shows up as a column
missing in production and present in every test.

`test_the_migrations_match_the_declaration` is the one that matters. The rest
cover the paths into a database that already exists.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.db.migrate import current_revision, upgrade_to_head
from app.db.schema import APP_TABLES, SDE_TABLES, apply_schema


def _url(path) -> str:
    return f"sqlite:///{path}"


def _tables(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# --- the two sources of truth agree ------------------------------------------

def test_the_migrations_match_the_declaration(tmp_path):
    """Migrate an empty database to head, then ask Alembic what is still missing.

    An empty answer means the history and `app/db/schema.py` describe the same
    database. A non-empty one means someone changed the declaration without
    generating a revision, and fresh installs are about to differ from upgraded
    ones — silently, because `apply_schema()` would create the new column on a
    fresh database and no migration would add it to an existing one.

    The fix when this fails is to generate the missing revision:
        alembic revision --autogenerate -m "what changed"
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from app.db.migrate import include_object
    from app.db.schema import metadata

    db = tmp_path / "head.db"
    upgrade_to_head(_url(db))

    engine = create_engine(_url(db))
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                opts={"include_object": include_object, "compare_type": True},
            )
            diff = compare_metadata(ctx, metadata)
    finally:
        engine.dispose()

    # SDE tables are deliberately outside the migration history, so a database
    # at head genuinely does not have them. include_object filters the tables;
    # their indexes arrive attached to a table object it has already rejected.
    diff = [d for d in diff if not _mentions_sde(d)]
    assert diff == [], (
        "the declaration and the migration history disagree:\n  "
        + "\n  ".join(str(d) for d in diff)
    )


def _mentions_sde(diff_entry) -> bool:
    return any(name in str(diff_entry) for name in SDE_TABLES)


def test_the_baseline_builds_every_app_table(tmp_path):
    db = tmp_path / "fresh.db"
    upgrade_to_head(_url(db))
    assert APP_TABLES <= _tables(db)


def test_the_baseline_leaves_static_data_alone(tmp_path):
    """CCP's tables are replaced wholesale on every SDE build.

    In the migration history they would mean a revision every time CCP adds a
    column to something we do not own, and a `DROP TABLE` in every downgrade.
    """
    db = tmp_path / "fresh.db"
    upgrade_to_head(_url(db))
    assert not (SDE_TABLES & _tables(db))


# --- getting an existing database under migration control --------------------

def test_a_pre_alembic_database_is_stamped_not_rebuilt(tmp_path):
    """Every install that exists today was built by CREATE TABLE IF NOT EXISTS.

    Running the baseline against one would try to create thirty-seven tables it
    already has. It is at the baseline by construction, so it gets recorded as
    such — and, critically, keeps its data.
    """
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    apply_schema(conn)
    conn.execute(
        "INSERT INTO characters (character_id, character_name, refresh_token, added_at) "
        "VALUES (?,?,?,?)", (95465499, "Astroasia", "refresh", 0.0))
    conn.commit()
    conn.close()

    assert current_revision(_url(db)) is None
    revision = upgrade_to_head(_url(db))
    assert revision is not None

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT character_name FROM characters").fetchall() == [("Astroasia",)]
    finally:
        conn.close()


def test_an_interrupted_first_migration_recovers(tmp_path):
    """Alembic creates `alembic_version` before it runs anything.

    So a first run that dies part-way leaves the table behind with no row in
    it. Reading "the table is there" as "Alembic has run here" sends that
    database down the upgrade path, where the baseline immediately fails on a
    table it already has. The question to ask is what revision is recorded, not
    whether the bookkeeping table exists — the same present-but-empty trap the
    SDE gate fell into.
    """
    db = tmp_path / "halfway.db"
    conn = sqlite3.connect(db)
    apply_schema(conn)
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    conn.commit()
    conn.close()

    assert upgrade_to_head(_url(db)) is not None


def test_migrating_twice_changes_nothing(tmp_path):
    """It runs on every startup, so it has to be free when there is nothing to do."""
    db = tmp_path / "twice.db"
    first = upgrade_to_head(_url(db))
    before = _tables(db)
    assert upgrade_to_head(_url(db)) == first
    assert _tables(db) == before


def test_an_explicit_url_is_never_overridden(tmp_path, monkeypatch):
    """env.py falls back to the app's database — it must not overrule a caller.

    This is not hypothetical: the first version of env.py set the URL
    unconditionally, and a migration aimed at a temporary database ran against
    the real one instead. Nothing was lost, but nothing would have stopped it.
    """
    target = tmp_path / "target.db"
    decoy = tmp_path / "decoy.db"
    monkeypatch.setenv("EVE_DATABASE_URL", _url(decoy))

    upgrade_to_head(_url(target))

    assert APP_TABLES <= _tables(target)
    assert not decoy.exists(), "the migration went to the configured database, not the one asked for"
