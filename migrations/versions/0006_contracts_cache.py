"""Contracts get a cache, so /contracts stops waiting on ESI to render.

Two tables with different lifetimes. `contracts_cache` is refreshed by the sync
worker like every other per-owner cache. `contract_items_cache` is written on
demand when a row is expanded and never refreshed at all: a contract's contents
are fixed when it is created, so the first read is permanently correct.

Idempotent for the reason 0002 had to become idempotent: `create_all` runs at
import in `app/db/database.py`, so on an existing install the tables are
already there by the time the startup handler reaches `upgrade_to_head()`.

Revision ID: f3a71d05c8be
Revises: e5c92a17b6d3
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a71d05c8be'
down_revision: Union[str, Sequence[str], None] = 'e5c92a17b6d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has('contracts_cache'):
        op.create_table(
            'contracts_cache',
            sa.Column('owner_id', sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column('owner_kind', sa.Text(), nullable=False),
            sa.Column('data_json', sa.Text(), nullable=False),
            sa.Column('cached_at', sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint('owner_id', 'owner_kind'),
        )
    if not _has('contract_items_cache'):
        op.create_table(
            'contract_items_cache',
            sa.Column('contract_id', sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column('data_json', sa.Text(), nullable=False),
            sa.Column('cached_at', sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint('contract_id'),
        )


def downgrade() -> None:
    for table in ('contract_items_cache', 'contracts_cache'):
        if _has(table):
            op.drop_table(table)
