"""The wallet journal and transactions get a cache, so /wallet stops fetching.

Same shape as 0004, with a division in the key: a corporation has seven wallet
divisions and the page shows one at a time, while a character has none and
sits at 0.

Idempotent for the reason 0002 had to become idempotent: `create_all` runs at
import in `app/db/database.py`, so on an existing install the table is already
there by the time the startup handler reaches `upgrade_to_head()`.

Revision ID: e5c92a17b6d3
Revises: d1e6b83c04af
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5c92a17b6d3'
down_revision: Union[str, Sequence[str], None] = 'd1e6b83c04af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has('wallet_ledger_cache'):
        return
    op.create_table(
        'wallet_ledger_cache',
        sa.Column('owner_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('owner_kind', sa.Text(), nullable=False),
        sa.Column('division', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('ledger', sa.Text(), nullable=False),
        sa.Column('data_json', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('owner_id', 'owner_kind', 'division', 'ledger'),
    )


def downgrade() -> None:
    if _has('wallet_ledger_cache'):
        op.drop_table('wallet_ledger_cache')
