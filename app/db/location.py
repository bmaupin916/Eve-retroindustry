"""Where the database is — one answer, for the app and for Alembic alike.

The path was computed inline in `app/db/database.py`, which was fine while
that was the only thing that needed it. Alembic needs the same answer from
outside the application, and importing `database.py` to get it would run
`create_all` as a side effect of asking a question.

`EVE_DATABASE_URL` is the seam the Postgres cutover goes through: nothing
else in the tree names a driver, so pointing this at a Postgres instance is
the whole of the switch as far as *addressing* is concerned.
"""

from __future__ import annotations

import os

# The deployment sets EVE_APP_DIR to a writable location; dev falls back to the
# project root.
APP_DIR = os.environ.get("EVE_APP_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

DB_PATH = os.path.join(APP_DIR, "eve_cache.db")


def database_url() -> str:
    """SQLAlchemy URL for the database this deployment uses."""
    return os.environ.get("EVE_DATABASE_URL") or f"sqlite:///{os.path.abspath(DB_PATH)}"


def is_sqlite() -> bool:
    """True while the store is still SQLite.

    Exists so the handful of places that legitimately need to know — batch
    ALTER in migrations, the bundled-SDE copy, `PRAGMA journal_mode` — can ask
    rather than assume. Everything else should not care.
    """
    return database_url().startswith("sqlite")
