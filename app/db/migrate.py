"""Bringing a database up to the current revision, including ones that predate Alembic.

Every install that exists right now was built by `CREATE TABLE IF NOT EXISTS`
statements scattered through the application, and has no `alembic_version`
table. Running the baseline migration against one of those would try to create
thirty-seven tables that are already there.

So there are two ways in, and the difference between them is decided by
evidence rather than by a flag someone has to remember to set:

* **`alembic_version` present** — ordinary case. Upgrade to head.
* **absent, but the app's tables are there** — a pre-Alembic database, already
  at the baseline by construction. Stamp it and do nothing else.
* **absent and empty** — a fresh install. Upgrade from nothing, which runs the
  baseline and builds everything.

The middle case is the one that only exists once. It stays because the same
situation arises for anyone restoring an old backup, and because a stamp that
runs when it should not is harmless while a baseline that runs when it should
not is a pile of "table already exists" errors on startup.
"""

from __future__ import annotations

import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.db.location import database_url
from app.db.schema import APP_TABLES, SDE_TABLES

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep CCP's static data out of the migration history.

    Lives here rather than in `migrations/env.py` because it is a fact about
    the schema, not about Alembic's environment — and because env.py runs its
    migrations at import time, so nothing can import it to ask what the rule
    is. The drift test needs exactly the same filter Alembic uses; two copies
    of it would be two chances to disagree.
    """
    if type_ == "table" and name in SDE_TABLES:
        return False
    if type_ == "index" and getattr(obj, "table", None) is not None:
        return obj.table.name not in SDE_TABLES
    return True


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(os.path.join(_REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "migrations"))
    # env.py reads the URL from app.db.location; this is only for the callers
    # that want to migrate a database other than the configured one (tests).
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def current_revision(url: str | None = None) -> str | None:
    engine = create_engine(url or database_url())
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def _looks_pre_alembic(url: str) -> bool:
    """True when the app's tables are present but no revision is recorded.

    Keyed on the *revision*, not on whether `alembic_version` exists. An
    interrupted first run leaves that table behind with no row in it, and
    reading its presence as "Alembic has run here" sends the database down the
    upgrade path to fail on the first `CREATE TABLE` for a table it already
    has — which is exactly what happened the first time this was tried.
    """
    if current_revision(url) is not None:
        return False
    engine = create_engine(url)
    try:
        existing = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    # Any substantial overlap means a real database rather than an empty file.
    # `characters` alone would do, but a database can legitimately have been
    # created and never logged into, so this asks more broadly.
    return len(APP_TABLES & existing) > 5


def upgrade_to_head(url: str | None = None) -> str | None:
    """Migrate the database to the current revision. Returns that revision.

    Safe to call on every startup: with nothing to do it is one query for the
    version and no writes.
    """
    url = url or database_url()
    cfg = alembic_config(url)
    if _looks_pre_alembic(url):
        # Already at the baseline by construction — record that, do not replay it.
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
    return current_revision(url)
