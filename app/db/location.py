"""Where the database is — one answer, for the app and for Alembic alike.

The path was computed inline in `app/db/database.py`, which was fine while
that was the only thing that needed it. Alembic needs the same answer from
outside the application, and importing `database.py` to get it would run
`create_all` as a side effect of asking a question.

`EVE_DATABASE_URL` is the seam the Postgres cutover goes through: nothing
else in the tree names a driver, so pointing this at a Postgres instance is
the whole of the switch as far as *addressing* is concerned.

**Everything here resolves per call. Nothing is frozen at import.** This module
and `app/web/deps.py` used to compute the path once, at import, from
`EVE_APP_DIR`. Any module imported before that variable was set therefore bound
whatever was there — and that is not hypothetical: the test suite spent a
session writing to a developer's real database, opening with `DELETE FROM
characters`, because `conftest` set the variable in a fixture and fixtures run
after collection has already imported every test module.

A function cannot be bound early. That is the entire reason these are functions
and not constants.
"""

from __future__ import annotations

import os

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def app_dir() -> str:
    """The writable data directory. A deployment sets EVE_APP_DIR; dev falls
    back to the project root."""
    return os.environ.get("EVE_APP_DIR") or _PROJECT_ROOT


def database_path() -> str:
    """Absolute path to the SQLite file. Reads the environment every time."""
    return os.path.join(app_dir(), "eve_cache.db")


def database_url() -> str:
    """SQLAlchemy URL for the database this deployment uses."""
    return (os.environ.get("EVE_DATABASE_URL")
            or f"sqlite:///{os.path.abspath(database_path())}")


def is_sqlite() -> bool:
    """True while the store is still SQLite.

    Exists so the handful of places that legitimately need to know — batch
    ALTER in migrations, the bundled-SDE copy, `PRAGMA journal_mode` — can ask
    rather than assume. Everything else should not care.
    """
    return database_url().startswith("sqlite")
