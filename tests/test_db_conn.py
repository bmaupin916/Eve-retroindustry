"""The connection layer the query rewrite targets.

`app/db/conn.py` is the seam: one way to open the database, named binds that
SQLAlchemy renders into whatever the driver speaks. This file was written ahead
of the move, when nothing used the seam and 316 statements were still written
against a raw `sqlite3.Connection` from `get_conn()`; it fixed the behaviour the
call sites would be moved onto, so the move ran against something already known
to work. **That move finished in v0.9.74** — the count is zero and these tests
now describe the layer everything uses, not one waiting for callers.

The Postgres half runs only when a server is reachable; see
`tests/test_postgres_schema.py` for how to start one.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.db import conn as db
from app.db.schema import apply_schema, upsert


@pytest.fixture
def sqlite_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 't.db'}"
    monkeypatch.setenv("EVE_DATABASE_URL", url)
    db.dispose()
    yield url
    db.dispose()


def test_named_binds_work(sqlite_url):
    """`:name` rather than `?`. The whole point: one statement, either driver."""
    with db.connect() as c:
        apply_schema(c.connection.driver_connection)
        c.execute(text(
            "INSERT INTO app_defaults (key, value) VALUES (:k, :v)"),
            {"k": "input_basis", "v": "buy"})
        c.commit()
        got = c.execute(text(
            "SELECT value FROM app_defaults WHERE key = :k"), {"k": "input_basis"})
        assert got.fetchone()[0] == "buy"


def test_rows_still_index_by_position(sqlite_url):
    """`row[0]` is how nearly every call site reads results.

    SQLAlchemy returns `Row`, not a tuple. If positional access did not work,
    the rewrite would be 316 statements *and* every reader of their results.
    """
    with db.connect() as c:
        apply_schema(c.connection.driver_connection)
        c.execute(text("INSERT INTO app_defaults (key, value) VALUES ('a', 'b')"))
        c.commit()
        row = c.execute(text("SELECT key, value FROM app_defaults")).fetchone()
        assert row[0] == "a" and row[1] == "b"
        assert tuple(row) == ("a", "b")


def test_the_engine_is_reused(sqlite_url):
    """An Engine owns a pool; making one per request would discard it each time."""
    assert db.engine() is db.engine()


def test_changing_the_url_rebuilds_the_engine(tmp_path, monkeypatch):
    """`EVE_DATABASE_URL` is the switch the Postgres cutover flips.

    A cached engine that ignored it would keep talking to the old database
    while every log line claimed otherwise.
    """
    db.dispose()
    monkeypatch.setenv("EVE_DATABASE_URL", f"sqlite:///{tmp_path / 'one.db'}")
    first = db.engine()
    monkeypatch.setenv("EVE_DATABASE_URL", f"sqlite:///{tmp_path / 'two.db'}")
    second = db.engine()
    assert first is not second
    assert str(second.url).endswith("two.db")
    db.dispose()


def test_sqlite_still_gets_its_pragmas(sqlite_url):
    """WAL and busy_timeout are what stopped "database is locked" when a
    character sync and a token refresh wrote at once. They are per-connection,
    so a pooled engine has to reapply them rather than set them once."""
    with db.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).fetchone()[0].lower() == "wal"
        assert c.execute(text("PRAGMA busy_timeout")).fetchone()[0] == 30000


def test_writes_need_an_explicit_commit(sqlite_url):
    """The one real behaviour difference from `sqlite3`, worth pinning loudly.

    SQLAlchemy opens a transaction on first use and rolls it back when the
    connection closes; `sqlite3` in its default isolation mode commits some
    statements for you. A call site moved across without its commit() will
    silently lose writes, so this states the rule rather than leaving it to be
    discovered.
    """
    with db.connect() as c:
        apply_schema(c.connection.driver_connection)
        c.execute(text("INSERT INTO app_defaults (key, value) VALUES ('x', 'y')"))
        # deliberately no commit

    with db.connect() as c:
        assert db.scalar(c, "SELECT value FROM app_defaults WHERE key='x'") is None


def test_scalar_returns_none_rather_than_raising(sqlite_url):
    with db.connect() as c:
        apply_schema(c.connection.driver_connection)
        assert db.scalar(c, "SELECT value FROM app_defaults WHERE key='nope'") is None
        assert db.scalar(c, "SELECT COUNT(*) FROM app_defaults") == 0


# --- the same code against Postgres -----------------------------------------

PG_URL = os.environ.get(
    "EVE_TEST_POSTGRES_URL",
    "postgresql+psycopg://eve:eve@localhost:5433/eve_retroindustry")


def _pg_reachable() -> bool:
    try:
        from sqlalchemy import create_engine
        e = create_engine(PG_URL, connect_args={"connect_timeout": 2})
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        e.dispose()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="no Postgres reachable")
def test_the_same_statement_runs_on_postgres(monkeypatch):
    """The claim this module exists to make, checked against both backends.

    Byte-for-byte the same SQL and the same parameter dict — no `?`, no `%s`,
    no branch on dialect at the call site.
    """
    from sqlalchemy import create_engine

    setup = create_engine(PG_URL)
    with setup.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS pytest_conn CASCADE"))
        c.execute(text("CREATE SCHEMA pytest_conn"))
        c.execute(text("CREATE TABLE pytest_conn.app_defaults "
                       "(key TEXT PRIMARY KEY, value TEXT)"))
        c.commit()
    setup.dispose()

    monkeypatch.setenv(
        "EVE_DATABASE_URL",
        PG_URL + ("&" if "?" in PG_URL else "?") + "options=-csearch_path%3Dpytest_conn")
    db.dispose()
    try:
        with db.connect() as c:
            c.execute(text("INSERT INTO app_defaults (key, value) VALUES (:k, :v)"),
                      {"k": "input_basis", "v": "buy"})
            c.commit()
            row = c.execute(text("SELECT key, value FROM app_defaults "
                                 "WHERE key = :k"), {"k": "input_basis"}).fetchone()
        assert row[0] == "input_basis" and row[1] == "buy"
    finally:
        db.dispose()


@pytest.mark.skipif(not _pg_reachable(), reason="no Postgres reachable")
def test_the_upsert_helper_is_usable_through_this_module(monkeypatch):
    """The inverse of what this test asserted until v0.9.80.

    It used to pin the `?` placeholders in place — `assert "?" in sql` — on the
    stated grounds that "its 316 callers pass tuples". The query conversion took
    that count to zero in v0.9.74, which left the assertion locking a
    non-portable form into a helper nothing called, and telling anyone who tried
    to fix it that the old behaviour was correct.

    Executed rather than string-matched, because the failure being guarded
    against is precisely that the output *looks* fine and does not run: `text()`
    reads `?` as literal SQL, so the old form raised `Incorrect number of
    bindings supplied` even on SQLite, and psycopg never accepted `?` at all.
    """
    from sqlalchemy import create_engine

    sql = upsert("app_defaults", ["key", "value"])
    assert "?" not in sql

    setup = create_engine(PG_URL)
    with setup.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS pytest_upsert CASCADE"))
        c.execute(text("CREATE SCHEMA pytest_upsert"))
        c.execute(text("CREATE TABLE pytest_upsert.app_defaults "
                       "(key TEXT PRIMARY KEY, value TEXT)"))
        c.commit()
    setup.dispose()

    monkeypatch.setenv(
        "EVE_DATABASE_URL",
        PG_URL + ("&" if "?" in PG_URL else "?")
        + "options=-csearch_path%3Dpytest_upsert")
    db.dispose()
    try:
        with db.connect() as c:
            c.execute(text(sql), {"key": "sell_venue", "value": "npc"})
            c.execute(text(sql), {"key": "sell_venue", "value": "upwell"})
            c.commit()
            got = c.execute(text("SELECT value FROM app_defaults WHERE key = :k"),
                            {"k": "sell_venue"}).scalar()
        assert got == "upwell", "the ON CONFLICT arm did not update the row"
    finally:
        db.dispose()


# ── the conversion does not have to be atomic ────────────────────────────────

def test_both_connection_styles_work_on_one_database(tmp_path, monkeypatch):
    """The claim the migration plan was built on turns out to be false.

    The design doc and the Step 4 worklist both said the flip was atomic:
    `get_conn()` is shared by every module, so the moment it returns a
    SQLAlchemy `Connection`, every one of the ~316 statements must already
    speak named binds. That framing made the last commit of the conversion a
    single change to the whole tree.

    It is not necessary. A converted module can take its connection from
    `app.db.conn.connect()` while everything else still calls `get_conn()` —
    same file, same process, both see each other's committed writes. So the
    conversion goes module by module, each independently shippable, and what
    is left at the end is deleting `get_conn()` once nothing calls it.
    """
    from app.web import deps

    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    db.dispose()

    legacy = deps.get_conn()          # raw sqlite3, positional placeholders
    try:
        legacy.execute(
            "INSERT INTO production_projects (name, created_at, updated_at)"
            " VALUES (?,?,?)", ("written by sqlite3", 1.0, 1.0))
        legacy.commit()

        with db.connect() as modern:  # SQLAlchemy, named binds
            seen = modern.execute(
                text("SELECT name FROM production_projects WHERE name = :n"),
                {"n": "written by sqlite3"}).fetchone()
            assert seen is not None, "the converted style cannot see legacy writes"

            modern.execute(
                text("INSERT INTO production_projects (name, created_at, updated_at)"
                     " VALUES (:n, :c, :u)"),
                {"n": "written by sqlalchemy", "c": 2.0, "u": 2.0})
            modern.commit()

        names = {r[0] for r in legacy.execute(
            "SELECT name FROM production_projects").fetchall()}
        assert names == {"written by sqlite3", "written by sqlalchemy"}
    finally:
        legacy.close()
        db.dispose()


def test_returning_replaces_lastrowid(sqlite_url):
    """`cursor.lastrowid` has no SQLAlchemy equivalent, and several call sites
    use it. `RETURNING` is the replacement, and it needs SQLite >= 3.35."""
    import sqlite3 as _sqlite3

    assert tuple(int(p) for p in _sqlite3.sqlite_version.split(".")) >= (3, 35), (
        f"SQLite {_sqlite3.sqlite_version} has no RETURNING; the conversion "
        "needs a different answer for lastrowid on this interpreter"
    )
    with db.connect() as conn:
        apply_schema(conn.connection.driver_connection)
        new_id = conn.execute(
            text("INSERT INTO production_projects (name, created_at, updated_at)"
                 " VALUES (:n, :c, :u) RETURNING id"),
            {"n": "p", "c": 1.0, "u": 1.0}).scalar()
        conn.commit()
        assert new_id == conn.execute(
            text("SELECT id FROM production_projects WHERE name = :n"), {"n": "p"}
        ).scalar()
