from sqlalchemy import create_engine, Column, Integer, String, Text, Float
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import NullPool
import json
import os

# Where the database lives is now answered in one place, because Alembic needs
# the same answer from outside the application and cannot import this module to
# get it — `create_all` below runs on import.
from app.db.location import database_path, database_url


class Base(DeclarativeBase):
    pass


class TypeCache(Base):
    __tablename__ = "type_cache"
    type_id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    group_id = Column(Integer)
    category_id = Column(Integer)


class BlueprintCache(Base):
    __tablename__ = "blueprint_cache"
    type_id = Column(Integer, primary_key=True)  # type_id produktu
    blueprint_type_id = Column(Integer)
    data_json = Column(Text, nullable=False)  # raw JSON z Fuzzwork API
    cached_at = Column(Float)  # unix timestamp


# NullPool: open a fresh sqlite3 connection per query. Avoids stale FDs after
# the SDE-download `shutil.move(...)` replaces eve_cache.db, which otherwise
# leaves pooled connections holding the old inode and raises
# "(sqlite3.OperationalError) attempt to write a readonly database"
# (SQLITE_READONLY_DBMOVED) on the next INSERT.
#
# **`timeout` is a `sqlite3.connect` argument and must not be sent to psycopg.**
# It was passed unconditionally until v0.9.75, so on Postgres this module raised
# `ProgrammingError: invalid connection option "timeout"` — at *import*, because
# `create_all` below connects. `app/web/main.py` imports `get_session` from
# here, so the whole application failed to start on Postgres and no page could
# be served. Nothing caught it: every route test binds SQLite, and the
# cross-backend files drive functions rather than the app.
_url = database_url()
_connect_args = (
    # 30s busy timeout so these writes don't raise "database is locked" when the
    # add-character background sync and token refreshes are writing concurrently.
    {"timeout": 30.0} if _url.startswith("sqlite") else {}
)
engine = create_engine(_url, poolclass=NullPool, connect_args=_connect_args)

# Both tables this declares (`type_cache`, `blueprint_cache`) are also in
# `app/db/schema.py` and therefore in the migration history, so on a migrated
# database this is a no-op that still costs a connection at import time. Left
# in place rather than removed here: it is the fresh-install path for a database
# that has never been migrated, and untangling that belongs in its own change.
Base.metadata.create_all(engine)


def harden_db_permissions() -> None:
    """Restrict eve_cache.db to the owning user.

    The DB holds every character's refresh token in `characters`. `.eve_config.json`
    has been chmod 0600 since forever (token_store.py) but only carries the client
    ID; the file with the actual secrets in it had no permissions set at all.
    Baseline finding 5.

    Called on import, because the SDE download replaces the file wholesale via
    shutil.move() and the replacement arrives with whatever permissions the temp
    file had. It used to be called from `ensure_user_tables()` as well — that
    function had no caller anywhere and went in v0.9.76, its job (recreating
    `type_cache` and `blueprint_cache` after the file is replaced) now done by
    `upgrade_to_head()` at startup, since both tables are in the migration
    history.

    On Windows this only clears the read-only bit — POSIX modes are not enforced —
    so it is effectively a no-op in desktop dev. The VPS is the target.
    """
    try:
        os.chmod(database_path(), 0o600)
    except OSError:
        pass  # missing file or a filesystem without modes; not worth failing startup


harden_db_permissions()


def get_session() -> Session:
    return Session(engine)