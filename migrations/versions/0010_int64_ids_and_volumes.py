"""EVE ids and market volumes that do not fit in 32 bits.

Follows 0009, which fixed `station_rigs.location_id` — the first column of this
class to be caught, and only because a converted module finally ran against
Postgres. The rest are here.

SQLite's INTEGER is a variable-width 64-bit type that treats the declaration as
advisory, so every one of these has been storing oversized values correctly for
the life of the project. A Postgres INTEGER is exactly 32 bits and raises
`NumericValueOutOfRange`.

**Which columns, and why each — measured rather than assumed.** ESI declares
essentially every integer field as int64, `type_id` and `group_id` included, so
the declared type cannot decide this: taken literally it says widen everything.
What decides it is the range the values actually occupy.

Widened because they overflow today:

* structure ids — 1,049,982,731,184 observed on a public contract, 489x the
  ceiling: `location_name_cache.location_id`, `station_volume_cache.location_id`,
  `facility_tax_cache.facility_id`, `public_contracts.start_location_id` and
  `.end_location_id`
* market volumes — 34,190,149,437 units of Tritanium traded in The Forge over
  seven days, 15.9x the ceiling, and 12,564,293,700 units sitting in Jita sell
  orders: `market_price_cache.volume` and `.jita_available`,
  `hub_price_cache.volume` and `.available`, `station_volume_cache.volume` and
  `.traded_volume`. These were not on the original list, which was about ids;
  they are the columns most likely to have bitten first, because minerals are
  most of what an industry tool prices.

Widened because they are nearly full, which is the same problem later:

* `character_id` across eight tables plus `pi_extractor_cache.char_id` and
  `public_contracts.issuer_id` — the highest id seen was 2,124,549,094, or
  98.9% of the ceiling, with ids minted continuously and roughly 23 million
  left. Waiting for this one to break means it breaks on whoever signs up next.
* `corporation_id` in `characters` and `corp_assets_cache` — same id space,
  2,042,491,468 observed, 95.1%.

**Deliberately left alone**, because they are nowhere near the ceiling and
widening by reflex costs clarity for nothing:

* `type_id` (371,027 in the SDE — 0.017%), `group_id`, `category_id`
* `region_id`, `solar_system_id`, `system_id`, `sys_a`, `sys_b` (30,030,141 —
  1.4%), `planet_id` (~4e7)
* `contract_id` (234,465,667 — 10.9%). Monotonic, so worth re-checking one day,
  but an order of magnitude of headroom is not urgent.
* `margin_snapshot.item_id` — **not an EVE id at all.** It is
  `margin_watchlist.id`, our own autoincrement row number. The name is the trap:
  a sweep matching on `item_id` would have widened it.
* every `id` primary key we mint ourselves, and the count columns — `me`, `te`,
  `runs`, `quantity`, `needed`, `purchased`, `step`, `division`, `jumps`.
  `quantity` is the one to watch: ESI declares it int64 and an asset stack can
  hold billions of units of a mineral, but nothing here was measured over the
  ceiling and a contract-item quantity is bounded by what fits in one contract.

`batch_alter_table` so this runs on both backends. It is a no-op in effect on
SQLite, but 0009 proved a migration that skips a backend leaves the declaration
and the history disagreeing there, which
`test_the_migrations_match_the_declaration` fails on.

Revision ID: d17e4a92b3c8
Revises: c05d38f1a9e2
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd17e4a92b3c8'
down_revision: Union[str, Sequence[str], None] = 'c05d38f1a9e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column) — nullability is read from the live table rather than
# hard-coded, so this stays correct if a column's nullability changes.
COLUMNS: list[tuple[str, str]] = [
    ("app_bootstrap", "character_id"),
    ("app_owner", "character_id"),
    ("app_sessions", "character_id"),
    ("char_assets_cache", "character_id"),
    ("char_blueprints_cache", "character_id"),
    ("char_skills_cache", "character_id"),
    ("char_wallet_cache", "character_id"),
    ("characters", "character_id"),
    ("characters", "corporation_id"),
    ("corp_assets_cache", "corporation_id"),
    ("pi_extractor_cache", "char_id"),
    ("location_name_cache", "location_id"),
    ("station_volume_cache", "location_id"),
    ("station_volume_cache", "volume"),
    ("station_volume_cache", "traded_volume"),
    ("facility_tax_cache", "facility_id"),
    ("public_contracts", "start_location_id"),
    ("public_contracts", "end_location_id"),
    ("public_contracts", "issuer_id"),
    ("market_price_cache", "volume"),
    ("market_price_cache", "jita_available"),
    ("hub_price_cache", "volume"),
    ("hub_price_cache", "available"),
]


def _retype(to_type, from_type) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table, column in COLUMNS:
        if not insp.has_table(table):
            continue
        existing = {c["name"]: c for c in insp.get_columns(table)}
        if column not in existing:
            continue
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column,
                               existing_type=from_type(),
                               type_=to_type(),
                               existing_nullable=existing[column]["nullable"])


def upgrade() -> None:
    _retype(sa.BigInteger, sa.Integer)


def downgrade() -> None:
    # Lossy by construction: a structure id or a mineral volume does not fit on
    # the way back down. Kept for symmetry, not because it is safe to run.
    _retype(sa.Integer, sa.BigInteger)
