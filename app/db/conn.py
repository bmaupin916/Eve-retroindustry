"""One way to open the database, whichever database it is.

This is the seam the Postgres move goes through. Today `get_conn()` in
`app/web/main.py` hands out a raw `sqlite3.Connection` and roughly 316
statements are written against it — positional `?` placeholders, tuple
parameters, `sqlite3.Row`-free tuple access. psycopg wants `%s` and dicts, so
none of that survives the move as written.

**Why this is an engine and not a compatibility shim.** Wrapping psycopg in
something that mimics `sqlite3` and rewrites `?` to `%s` would be less work.
It would also make the DBAPI the application's internal contract, which is the
thing Step 5 then has to fight: the account-scoped query layer that
multi-tenancy needs wants to compose and inspect statements, not hand strings
to a driver. Better to have one abstraction than two.

**Placeholders.** SQLAlchemy renders named binds — `:type_id` — into whatever
the driver actually speaks, which is what makes one statement run on both
backends. That is the change the 316 call sites are waiting for; nothing here
converts them, and both styles work side by side while they move:

    conn.execute(text("SELECT name FROM sde_types WHERE type_id = :tid"),
                 {"tid": 34})

**SQLite pragmas.** WAL and a long busy timeout are what stopped "database is
locked" when a character sync and a token refresh wrote at the same time. They
have to be reapplied to every pooled connection rather than once at startup,
so they hang off a connect event instead of being called by hand.
"""

from __future__ import annotations

import sqlite3

import os
from functools import lru_cache

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from app.db.location import database_url, is_sqlite

_engine: Engine | None = None
_engine_url: str | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    """Per-connection pragmas. See the module docstring for why WAL matters.

    Wrapped because the same engine is used against `:memory:` in tests, where
    journal_mode is meaningless and setting it is not an error worth failing a
    connection over.
    """
    if not isinstance(dbapi_conn, sqlite3.Connection):
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    finally:
        cur.close()


#: "That table is not in this database." SQLite raises OperationalError ("no
#: such table"); Postgres raises ProgrammingError (UndefinedTable). Code that
#: catches the *driver's* exception class keeps working right up until the
#: driver changes, and then fails silently — the fallback simply stops running.
NO_SUCH_TABLE = (OperationalError, ProgrammingError)


def recover_from_missing_table(conn: Connection) -> list:
    """Roll back after a missing-table error and return the empty answer.

    The rollback is not tidiness. Postgres aborts the entire transaction on any
    failed statement and refuses every later one with InFailedSqlTransaction
    until it is rolled back. So the `try/except: return []` idiom — which this
    codebase uses in a dozen places to tolerate an older SDE — works perfectly
    on SQLite and leaves the connection unusable on Postgres, with the damage
    appearing in whatever unrelated query happens to run next.
    """
    try:
        conn.rollback()
    except Exception:
        pass
    return []


def engine(url: str | None = None) -> Engine:
    """The process-wide engine, rebuilt if the configured URL changes.

    Cached because an Engine owns a connection pool and creating one per
    request would throw the pool away each time — the opposite of the point.
    """
    global _engine, _engine_url
    target = url or database_url()
    if _engine is not None and _engine_url == target:
        return _engine

    if _engine is not None:
        _engine.dispose()

    if target.startswith("sqlite"):
        # NullPool: a fresh file handle per connection. The fresh-install path
        # still replaces eve_cache.db wholesale, and a pooled handle onto the
        # old inode raises SQLITE_READONLY_DBMOVED on the next write.
        new = _create(target, poolclass=NullPool, connect_args={"timeout": 30.0})
        event.listen(new, "connect", _configure_sqlite)
    else:
        # Postgres pools properly. pre_ping because a server restart or an idle
        # timeout otherwise surfaces as a failed request rather than a
        # reconnect.
        new = _create(target, pool_pre_ping=True, pool_size=5, max_overflow=5)

    _engine, _engine_url = new, target
    return new


def _create(url: str, **kwargs) -> Engine:
    from sqlalchemy import create_engine
    return create_engine(url, future=True, **kwargs)


def connect(url: str | None = None) -> Connection:
    """A connection from the engine. Close it, or use it as a context manager.

    Note the transaction difference from `sqlite3`, which is the one thing that
    bites when moving a call site: SQLAlchemy begins a transaction on first
    use, so writes need an explicit `conn.commit()`. `sqlite3` in its default
    isolation mode commits some statements for you.
    """
    return engine(url).connect()


def _configure_borrowed_sqlite(dbapi_conn, _record) -> None:
    """Pragmas for a database we were merely *pointed at* — busy timeout only.

    Deliberately **not** `journal_mode=WAL`. That setting is a property of the
    file, not of the connection: applying it rewrites the database header and
    persists. `connect_to_path` is used for the SDE, and `sde_base.db` is a
    committed 10 MB binary — so applying the whole set of them flipped it from
    `delete` to `wal`, dirtied it in git on every test run, and would have
    shipped a database that can no longer be opened read-only, because WAL
    needs to create `-wal`/`-shm` sidecars beside the file.

    The callers here only read (see `tests/test_sqlite_under_the_worker.py`,
    which scans for exactly that), so none of what WAL buys is needed.
    """
    if not isinstance(dbapi_conn, sqlite3.Connection):
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    finally:
        cur.close()


@lru_cache(maxsize=8)
def _engine_for_path(db_path: str) -> Engine:
    engine = create_engine(f"sqlite:///{os.path.abspath(db_path)}", poolclass=NullPool)
    event.listen(engine, "connect", _configure_borrowed_sqlite)
    return engine


def connect_to_path(db_path: str) -> Connection:
    """A connection to *this* database file, not the configured one.

    For the call sites that are handed an explicit path — the BOM resolver's
    callers, which take `db_path` precisely so the caller decides which
    database. Routing them through `connect()` instead would quietly ignore the
    argument and open whatever `EVE_DATABASE_URL`/`EVE_APP_DIR` happens to say,
    which is correct in the app and wrong in every test that builds a
    throwaway database in a tmp directory.

    Engines are cached per path because an Engine owns a pool; NullPool keeps
    the old one-connection-per-call behaviour so nothing holds a file handle
    open between calls.
    """
    return _engine_for_path(db_path).connect()


def dispose() -> None:
    """Drop the pool. For tests, and for the paths that replace the database file."""
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine, _engine_url = None, None


def scalar(conn: Connection, sql: str, params: dict | None = None):
    """First column of the first row, or None.

    The single most common shape in this codebase — `.fetchone()` followed by
    indexing `[0]` and a None check — written once so the call sites stop
    repeating it.
    """
    row = conn.execute(text(sql), params or {}).fetchone()
    return row[0] if row else None
