"""station_rigs.location_id holds a structure id, which does not fit in an INTEGER.

An Upwell structure id is around 1.03e12. SQLite stores it happily whatever the
column says — its INTEGER is a variable-width 64-bit type and the declaration is
advisory — so this was invisible for as long as SQLite was the only backend. On
Postgres an INTEGER is exactly 32 bits, so saving a rig configuration for any
player-owned structure fails with `NumericValueOutOfRange`, and player-owned
structures are the only ones that can carry rigs at all.

Found by running the `industry_helper` rig tests against Postgres for the first
time: eight failures there, none on SQLite. That asymmetry is the whole reason
the cross-backend files exist.

This fixes the one column that blocked that slice. It is not the only column of
its kind — ESI declares character, corporation, location, item, order and
contract ids as int64 — and the rest are listed in docs/working-notes.md
rather than swept into this migration, because each needs deciding on its own
and a widening ALTER on a live table is not something to do by pattern match.

Runs on SQLite too, through `batch_alter_table`. Semantically it changes nothing
there — the stored values are already 64-bit — but `test_the_migrations_match_
the_declaration` compares the migrated schema against the declaration on the
backend it runs, and a migration that quietly skips one of them leaves the two
disagreeing. It caught exactly that when this first skipped SQLite.

Revision ID: c05d38f1a9e2
Revises: b94c27ae51f8
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c05d38f1a9e2'
down_revision: Union[str, Sequence[str], None] = 'b94c27ae51f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table('station_rigs'):
        return
    with op.batch_alter_table('station_rigs') as batch:
        batch.alter_column('location_id',
                           existing_type=sa.Integer(),
                           type_=sa.BigInteger(),
                           existing_nullable=False)


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table('station_rigs'):
        return
    # Lossy by construction: any real structure id stored here overflows on the
    # way back down. Kept for symmetry, not because it is safe to run.
    with op.batch_alter_table('station_rigs') as batch:
        batch.alter_column('location_id',
                           existing_type=sa.BigInteger(),
                           type_=sa.Integer(),
                           existing_nullable=False)
