"""Sync events — an append-only log of what the worker found had changed.

The background sync worker refreshes caches. That is enough for the web UI,
which re-reads them on every page load, but §9.5 of the design doc wants a
Discord bot that *announces* things — and a bot that polls a cache for changes
misses them: two changes between polls look like one, and a change that reverts
looks like none.

An append-only table with a monotonic id is the only shape that works for both
backends and across a restart. A consumer keeps a cursor and asks for
`id > cursor`, which is exactly "what did I miss while I was down?". Postgres
LISTEN/NOTIFY can be layered on later as a wake-up signal; the log stays the
source of truth.

Revision ID: b3f2a1c47d90
Revises: 5c9156e72c43
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f2a1c47d90'
down_revision: Union[str, Sequence[str], None] = '5c9156e72c43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    """Whether the table is already there.

    It legitimately can be. `ensure_schema()` creates every declared table with
    `CREATE TABLE IF NOT EXISTS` on the first connection of the process, and
    `app/db/database.py` runs `create_all` at import — so on an existing
    install the table can exist before Alembic is asked to add it. A migration
    that assumes otherwise fails on every upgrade of a running deployment,
    which is exactly what happened the first time this one ran.
    """
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has('sync_events'):
        # Nothing to build; the revision still gets stamped, which is the point.
        return

    op.create_table(
        'sync_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('character_id', sa.BigInteger(), nullable=True),
        sa.Column('corporation_id', sa.BigInteger(), nullable=True),
        sa.Column('detail_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_sync_events_id_kind', 'sync_events', ['id', 'kind'],
                    unique=False)
    op.create_index('idx_sync_events_character', 'sync_events',
                    ['character_id', 'id'], unique=False)


def downgrade() -> None:
    if not _has('sync_events'):
        return
    op.drop_index('idx_sync_events_character', table_name='sync_events')
    op.drop_index('idx_sync_events_id_kind', table_name='sync_events')
    op.drop_table('sync_events')
