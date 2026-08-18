"""The bundled-SDE refresh gate.

Refreshing is decided by comparing the user's ``eve_cache.db`` against the
bundled ``sde_base.db``. The comparison has to notice three different ways the
user's copy can be behind, and the cheap one (row counts) misses two of them.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

TYPES_DDL_OLD = (
    "CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
    "group_id INTEGER, published INTEGER DEFAULT 1, market_group_id INTEGER)"
)
TYPES_DDL_NEW = TYPES_DDL_OLD[:-1] + ", volume REAL)"


def _make(path: str, ddl: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    conn.execute("CREATE TABLE sde_groups (group_id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany(
        f"INSERT INTO sde_types VALUES ({','.join('?' * len(rows[0]))})", rows)
    conn.execute("INSERT INTO sde_groups VALUES (18, 'Mineral')")
    conn.commit()
    conn.close()


@pytest.fixture
def gate(app_module, tmp_path, monkeypatch):
    """Returns (run, user_conn) where `run` refreshes from a bundle you build."""
    def run(bundle_ddl: str, bundle_rows: list[tuple], user_ddl: str,
            user_rows: list[tuple]) -> tuple[int, sqlite3.Connection]:
        bundle = str(tmp_path / "sde_base.db")
        user = str(tmp_path / "eve_cache.db")
        for p in (bundle, user):
            if os.path.exists(p):
                os.remove(p)
        _make(bundle, bundle_ddl, bundle_rows)
        _make(user, user_ddl, user_rows)
        monkeypatch.setattr(app_module, "_bundled_sde_path", lambda: bundle)
        conn = sqlite3.connect(user)
        return app_module._refresh_sde_from_bundle(conn), conn
    return run


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(sde_types)")}


def test_identical_databases_are_left_alone(gate):
    rows = [(34, "Tritanium", 18, 1, 1857)]
    count, conn = gate(TYPES_DDL_OLD, rows, TYPES_DDL_OLD, rows)
    assert count == 1
    assert _columns(conn) == {"type_id", "name", "group_id", "published",
                              "market_group_id"}


def test_more_types_in_the_bundle_triggers_a_refresh(gate):
    count, conn = gate(
        TYPES_DDL_OLD, [(34, "Tritanium", 18, 1, 1857), (35, "Pyerite", 18, 1, 1857)],
        TYPES_DDL_OLD, [(34, "Tritanium", 18, 1, 1857)])
    assert count == 2


def test_a_new_column_triggers_a_refresh_even_at_equal_row_counts(gate):
    """The v0.9.23 case: sde_types.volume arrives with the same 1 type in it.

    Row counts and table names are identical, so only a column-level check can
    see that the user's copy is behind — without it the margin tracker's
    profit-per-m3 stays permanently hidden on every existing install.
    """
    count, conn = gate(
        TYPES_DDL_NEW, [(34, "Tritanium", 18, 1, 1857, 0.01)],
        TYPES_DDL_OLD, [(34, "Tritanium", 18, 1, 1857)])
    assert count == 1
    assert "volume" in _columns(conn)
    assert conn.execute(
        "SELECT volume FROM sde_types WHERE type_id=34").fetchone()[0] == 0.01


def test_a_column_only_the_user_has_is_not_treated_as_stale(gate):
    """Staleness is one-directional. A user column absent from the bundle must
    not loop the refresh on every startup."""
    count, conn = gate(
        TYPES_DDL_OLD, [(34, "Tritanium", 18, 1, 1857)],
        TYPES_DDL_NEW, [(34, "Tritanium", 18, 1, 1857, 0.01)])
    assert count == 1
    assert "volume" in _columns(conn)     # untouched, not dropped
