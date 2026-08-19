"""Custom container names get a cache — the last ESI call on /assets.

Keyed on the item alone. A container's name belongs to the container; which
character asked matters only at fetch time, and that is the sync worker's
problem now.

Idempotent for the reason 0002 had to become idempotent: `create_all` runs at
import in `app/db/database.py`, so on an existing install the table is already
there by the time the startup handler reaches `upgrade_to_head()`.

Revision ID: a82f4e60d197
Revises: f3a71d05c8be
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a82f4e60d197'
down_revision: Union[str, Sequence[str], None] = 'f3a71d05c8be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has('container_name_cache'):
        return
    op.create_table(
        'container_name_cache',
        sa.Column('item_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('item_id'),
    )


def downgrade() -> None:
    if _has('container_name_cache'):
        op.drop_table('container_name_cache')
