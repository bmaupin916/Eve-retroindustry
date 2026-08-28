"""Materialised market KPIs — §9.4's volatility, trend and competition.

The three remaining cache-only KPIs all need the daily series, and until
v0.9.86 nothing filled it: `price_history_cache` held zero rows because the
only writer was a user opening a price chart. With the background fill in
place there is finally something to compute from.

**Why a table rather than computing on read.** §9.4 asks for it, and the reason
is the shape of the read: `/prices/groups` aggregates a whole branch, so a
market group with four hundred types under it would parse four hundred JSON
blobs per page load, every load. The stats are recomputed once when the history
behind them moves, which is at most daily per type.

**`days` is not a detail.** It records how many trading days the window actually
held. An illiquid item that traded three days out of thirty gets a volatility
figure from three observations; without that column a consumer would render it
next to one computed from thirty as if they were the same measurement — which
is precisely the failure the reactions board hit when it ranked a booster on a
price with one real order behind it.

Revision ID: a3f1c7d20e94
Revises: d17e4a92b3c8
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f1c7d20e94'
down_revision: Union[str, Sequence[str], None] = 'd17e4a92b3c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    # `app/db/database.py` calls `create_all` at import, so on an existing
    # deployment this table is already there by the time the startup handler
    # reaches `upgrade_to_head()`. Written the obvious way this raises, the
    # revision is never stamped, and the app logs MIGRATION FAILED on every
    # restart afterwards — which is what the first version of 0002 did.
    if _has('market_stats'):
        return
    op.create_table(
        "market_stats",
        sa.Column("region_id", sa.Integer(), nullable=False),
        sa.Column("type_id", sa.Integer(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("avg_daily_volume", sa.Float(), nullable=True),
        sa.Column("volatility_pct", sa.Float(), nullable=True),
        sa.Column("trend_pct", sa.Float(), nullable=True),
        sa.Column("avg_order_count", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("region_id", "type_id"),
    )


def downgrade() -> None:
    # Derived data with a single source. Dropping it costs one recomputation
    # from history that is still on disk, so there is nothing to preserve.
    if _has('market_stats'):
        op.drop_table("market_stats")
