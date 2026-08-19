"""Colonies get a cache, so /planets and /pi-planner stop waiting on ESI.

The most call-hungry pages in the app: one colony-list call per character plus
one detail call per planet, on every view.

Idempotent for the reason 0002 had to become idempotent: `create_all` runs at
import in `app/db/database.py`, so on an existing install the table is already
there by the time the startup handler reaches `upgrade_to_head()`.

Revision ID: b94c27ae51f8
Revises: a82f4e60d197
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b94c27ae51f8'
down_revision: Union[str, Sequence[str], None] = 'a82f4e60d197'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has('pi_colony_cache'):
        return
    op.create_table(
        'pi_colony_cache',
        sa.Column('char_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('data_json', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('char_id'),
    )


def downgrade() -> None:
    if _has('pi_colony_cache'):
        op.drop_table('pi_colony_cache')
