"""Alembic environment.

Two things worth knowing before editing this file.

**Only the app's own tables are under migration control.** `sde_*` is CCP's
static data: it is dropped and rebuilt wholesale on every SDE build, so
putting it in the migration history would mean a revision every time CCP adds
a column to something we do not own. `app/db/schema.py` creates those tables
directly through `apply_sde_schema()`; `include_object` below keeps them out
of autogenerate so they are never seen as drift.

**The URL comes from the application, not from alembic.ini.** A URL written in
two places is a URL that will disagree in one of them, and the one that would
be wrong is whichever is not being read at the time.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.location import database_url
from app.db.migrate import include_object
from app.db.schema import metadata

config = context.config

# Fall back to the application's URL, but never override one a caller has
# already set. `alembic -x`, and `app.db.migrate` migrating a database other
# than the configured one, both pass an explicit URL — and silently ignoring it
# means running a migration against the wrong database, which is the one
# mistake in here with no undo.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _configure(**kwargs):
    context.configure(
        target_metadata=target_metadata,
        include_object=include_object,
        # SQLite cannot ALTER most things, so Alembic rebuilds the table
        # instead. Harmless on Postgres and required until the port lands.
        render_as_batch=database_url().startswith("sqlite"),
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
