"""Industry jobs get a cache, so /jobs stops waiting on ESI to render.

Same shape as the asset and blueprint caches beside it. Idempotent for the
reason 0002 had to become idempotent: `create_all` runs at import in
`app/db/database.py`, so on an existing install the table is already there by
the time the startup handler reaches `upgrade_to_head()`.

Revision ID: c7a4e91b2fd6
Revises: b3f2a1c47d90
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7a4e91b2fd6'
down_revision: Union[str, Sequence[str], None] = 'b3f2a1c47d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has('char_jobs_cache'):
        return
    op.create_table(
        'char_jobs_cache',
        sa.Column('character_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('data_json', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('character_id'),
    )


def downgrade() -> None:
    if _has('char_jobs_cache'):
        op.drop_table('char_jobs_cache')
