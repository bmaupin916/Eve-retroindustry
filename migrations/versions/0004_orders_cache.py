"""Market orders get a cache, so /orders stops waiting on ESI to render.

Same story as 0003 and the same shape, with a wider key: the page selects on
whose orders and whether they are live, so the cache is keyed the same way
rather than split across four tables.

Idempotent for the reason 0002 had to become idempotent: `create_all` runs at
import in `app/db/database.py`, so on an existing install the table is already
there by the time the startup handler reaches `upgrade_to_head()`.

Revision ID: d1e6b83c04af
Revises: c7a4e91b2fd6
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1e6b83c04af'
down_revision: Union[str, Sequence[str], None] = 'c7a4e91b2fd6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has('market_orders_cache'):
        return
    op.create_table(
        'market_orders_cache',
        sa.Column('owner_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('owner_kind', sa.Text(), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('data_json', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('owner_id', 'owner_kind', 'state'),
    )


def downgrade() -> None:
    if _has('market_orders_cache'):
        op.drop_table('market_orders_cache')
