"""Every table this application owns, declared once, in one file.

Before this module the schema lived in twenty `ensure_*()` functions spread
across fourteen modules, half of them called at startup and half lazily on
first use, plus two tables in SQLAlchemy models and fourteen more in
`import_sde.py`. Nothing anywhere said what the database looked like, which
had three consequences worth naming because they are what this module exists
to stop:

1. **Tables appeared at different times on different installs.** `public_*`
   and `route_jump_cache` only exist once you visit the page that creates
   them, so two deployments of the same version had different schemas.
2. **There was no baseline to migrate from.** Alembic needs a starting point,
   and "whatever tables happen to have been created by the pages you have
   visited" is not one.
3. **The DDL was SQLite-specific**, so a Postgres port meant hand-translating
   thirty-four statements and keeping both versions in step forever.

Declaring the schema through SQLAlchemy Core rather than as SQL strings fixes
(3) properly instead of hedging it: one declaration emits `INTEGER PRIMARY
KEY` on SQLite and `SERIAL` on Postgres, and Alembic can autogenerate against
it. Nothing else in the app is being converted to the ORM — the ~316 queries
stay as hand-written SQL on `sqlite3.Connection`. This module owns *shape*,
not access.

Two scopes, because they have genuinely different lifecycles:

* `apply_schema()` — tables this app writes. Owned by Alembic; migrated,
  never dropped.
* `apply_sde_schema()` — CCP's static data. Replaced wholesale on every SDE
  build and safe to drop, so it is deliberately outside the migration story.

**Portability notes, applied throughout.** Natural-key primary keys carry
`autoincrement=False`: an EVE character ID is supplied by CCP, and SQLAlchemy
would otherwise make it a `SERIAL` on Postgres and attach a sequence that
must never be used. Epoch-seconds columns stored as integers are
`BigInteger`, because Postgres `INTEGER` is four bytes and stops holding a
unix timestamp in 2038. And the `DEFAULT (strftime('%s','now'))` server
defaults from the old DDL are gone — they are SQLite-only syntax, and a
timestamp filled in silently by the database is the kind of implicit
behaviour that changes meaning across dialects. Every writer now states its
own time.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

# One MetaData so Alembic has a single `target_metadata`. The app/SDE split is
# carried by the table-name sets below rather than by separate registries.
metadata = MetaData()


# ───────────────────────────────────────────────────────────────────────────
# Characters, sessions and instance ownership
# ───────────────────────────────────────────────────────────────────────────

characters = Table(
    "characters", metadata,
    Column("character_id", Integer, primary_key=True, autoincrement=False),
    Column("character_name", Text, nullable=False),
    # Refresh tokens are bound to the client ID that issued them: change
    # EVE_CLIENT_ID and every row here becomes unusable, which is why the
    # first-run setup page is a one-time thing rather than an editable field.
    Column("refresh_token", Text, nullable=False),
    Column("access_token", Text),
    Column("token_expires_at", Float),
    Column("corporation_id", Integer),
    Column("last_sync_at", Float),
    Column("added_at", Float, nullable=False),
)

app_sessions = Table(
    "app_sessions", metadata,
    Column("session_id", Text, primary_key=True),
    Column("character_id", Integer, nullable=False),
    Column("csrf_token", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("last_seen_at", Float, nullable=False),
)

app_owner = Table(
    "app_owner", metadata,
    # Single-row table: the first character to log in claims the instance.
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("character_id", Integer, nullable=False),
    Column("claimed_at", Float, nullable=False),
    CheckConstraint("id = 1", name="ck_app_owner_single_row"),
)

app_bootstrap = Table(
    "app_bootstrap", metadata,
    # Single-use, ten-minute login links minted by `python -m app.web.bootstrap`
    # — the way back in when the SSO callback registration does not match.
    Column("token", Text, primary_key=True),
    Column("character_id", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
)

app_defaults = Table(
    "app_defaults", metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
)


# ───────────────────────────────────────────────────────────────────────────
# Per-character ESI caches
# ───────────────────────────────────────────────────────────────────────────
#
# The three `data_json` caches below have no primary key, by inheritance
# rather than by design: they are written with DELETE-then-INSERT. That makes
# every read a full scan and every write unable to use ON CONFLICT, so they
# are the first candidates when the sync worker lands in this step.

char_assets_cache = Table(
    "char_assets_cache", metadata,
    Column("character_id", Integer, nullable=False),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float),
)

corp_assets_cache = Table(
    "corp_assets_cache", metadata,
    Column("corporation_id", Integer, nullable=False),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float),
)

char_blueprints_cache = Table(
    "char_blueprints_cache", metadata,
    Column("character_id", Integer, nullable=False),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float),
)

char_jobs_cache = Table(
    "char_jobs_cache", metadata,
    # Industry jobs. Cached like assets and blueprints so /jobs renders without
    # waiting on ESI — the background worker keeps it warm.
    Column("character_id", BigInteger, primary_key=True, autoincrement=False),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float, nullable=False),
)

char_skills_cache = Table(
    "char_skills_cache", metadata,
    Column("character_id", Integer, primary_key=True, autoincrement=False),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float, nullable=False),
)

char_wallet_cache = Table(
    "char_wallet_cache", metadata,
    # A character's ISK balance. Kept warm by the sync worker, which is why the
    # dashboard's five-minute TTL read below never has to fall through to ESI
    # any more. Balances stay here rather than moving into the ledger table
    # next to it: two places holding the same number is how they come to
    # disagree, and this one already has a second consumer.
    Column("character_id", Integer, primary_key=True, autoincrement=False),
    Column("balance", Float, nullable=False),
    Column("cached_at", Float, nullable=False),
)

wallet_ledger_cache = Table(
    "wallet_ledger_cache", metadata,
    # The journal and the transaction list, so /wallet renders without waiting
    # on ESI — up to 2,500 rows each, and the journal is paginated, so this was
    # the most expensive page in the app to open.
    #
    # `division` is 0 for a character, which has no divisions, and 1–7 for a
    # corporation, which has seven and shows one at a time. Corporation
    # *balances* arrive as one list covering every division, so they are stored
    # once at division 0 under `balances` rather than split across seven rows
    # that would all have to be written together to stay consistent.
    Column("owner_id", BigInteger, primary_key=True, autoincrement=False),
    Column("owner_kind", Text, primary_key=True),     # "character" | "corporation"
    Column("division", Integer, primary_key=True, autoincrement=False),
    Column("ledger", Text, primary_key=True),         # journal | transactions | balances
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float, nullable=False),
)

market_orders_cache = Table(
    "market_orders_cache", metadata,
    # Market orders, so /orders renders without waiting on ESI.
    #
    # **One table rather than four.** The page has two switches — whose orders
    # (`?scope=`) and whether they are live (`?state=`) — and the four
    # combinations are the same shape: a list of order dicts. Four tables would
    # be four migrations and four fetchers for one concept, and the key here
    # mirrors what the page already asks for.
    #
    # `owner_kind` rather than inferring from the id: character and corporation
    # ids come from ranges that do not currently collide, and building a cache
    # on "currently" is how you get a corp's orders shown as a character's.
    Column("owner_id", BigInteger, primary_key=True, autoincrement=False),
    Column("owner_kind", Text, primary_key=True),      # "character" | "corporation"
    Column("state", Text, primary_key=True),           # "active" | "history"
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float, nullable=False),
)


# ───────────────────────────────────────────────────────────────────────────
# Market data
# ───────────────────────────────────────────────────────────────────────────

market_price_cache = Table(
    "market_price_cache", metadata,
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("sell_price", Float),
    Column("buy_price", Float),
    Column("cached_at", Float),
    # Both arrived as ALTER TABLE ADD COLUMN guarded by a PRAGMA probe. They
    # are ordinary columns of the baseline now.
    Column("volume", Integer),
    Column("jita_available", Integer),
)

custom_price_override = Table(
    "custom_price_override", metadata,
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("price", Float, nullable=False),
    Column("updated_at", Float),
)

hub_price_cache = Table(
    "hub_price_cache", metadata,
    # Secondary trade hubs (Amarr / Dodixie / Rens / Hek), fetched on demand.
    # Region-wide best sell/buy plus 7-day region volume.
    Column("region_id", Integer, primary_key=True, autoincrement=False),
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("sell_price", Float),
    Column("buy_price", Float),
    Column("volume", Integer),
    Column("available", Integer),
    Column("cached_at", Float),
)

price_history_cache = Table(
    "price_history_cache", metadata,
    # Full daily market history (~1 year) per (region, type), for the chart.
    Column("region_id", Integer, primary_key=True, autoincrement=False),
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float),
)

station_volume_cache = Table(
    "station_volume_cache", metadata,
    Column("location_id", Integer, primary_key=True, autoincrement=False),
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("volume", Integer),
    Column("best_sell", Float),
    Column("traded_volume", Integer),
    Column("cached_at", Float),
)

market_hist_etag = Table(
    "market_hist_etag", metadata,
    Column("region_id", Integer, primary_key=True, autoincrement=False),
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("etag", Text),
    Column("days_json", Text),
    Column("cached_at", Float),
    Column("expires_at", Float),
)


# ───────────────────────────────────────────────────────────────────────────
# Industry: job cost inputs and structure modelling
# ───────────────────────────────────────────────────────────────────────────

adjusted_price_cache = Table(
    "adjusted_price_cache", metadata,
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("adjusted", Float, nullable=False),
    Column("cached_at", BigInteger, nullable=False),
)

sci_cache = Table(
    "sci_cache", metadata,
    # System cost index, per (system, activity). Manufacturing and reaction
    # carry different indices in the same system.
    Column("solar_system_id", Integer, primary_key=True, autoincrement=False),
    Column("activity", Text, primary_key=True),
    Column("cost_index", Float, nullable=False),
    Column("cached_at", BigInteger, nullable=False),
)

facility_tax_cache = Table(
    "facility_tax_cache", metadata,
    Column("facility_id", Integer, primary_key=True, autoincrement=False),
    Column("tax_rate", Float, nullable=False),
    Column("cached_at", BigInteger, nullable=False),
)

station_rigs = Table(
    "station_rigs", metadata,
    Column("location_id", Integer, primary_key=True, autoincrement=False),
    Column("me_bonus_pct", Float, nullable=False, server_default="0"),
    Column("updated_at", BigInteger, nullable=False),
    Column("structure_type", Text),
    Column("rig1_type_id", Integer),
    Column("rig2_type_id", Integer),
    Column("rig3_type_id", Integer),
)

rig_bonuses = Table(
    "rig_bonuses", metadata,
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
    Column("set_size", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("me_bonus", Float, nullable=False, server_default="0"),
    Column("te_bonus", Float, nullable=False, server_default="0"),
)


# ───────────────────────────────────────────────────────────────────────────
# Universe name and route caches
# ───────────────────────────────────────────────────────────────────────────

location_name_cache = Table(
    "location_name_cache", metadata,
    Column("location_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
    Column("solar_system_id", Integer),
    # `region_id` existed only as an ALTER — it was never in any CREATE TABLE,
    # so a database built from scratch by the old code did not have it and a
    # database that had been upgraded did.
    Column("region_id", Integer),
)

solar_system_cache = Table(
    "solar_system_cache", metadata,
    Column("system_id", Integer, primary_key=True, autoincrement=False),
    Column("security_status", Float),
    Column("cached_at", BigInteger, nullable=False),
)

route_jump_cache = Table(
    "route_jump_cache", metadata,
    Column("sys_a", Integer, primary_key=True, autoincrement=False),
    Column("sys_b", Integer, primary_key=True, autoincrement=False),
    Column("jumps", Integer, nullable=False),
    Column("cached_at", Float),
)


# ───────────────────────────────────────────────────────────────────────────
# Planetary interaction
# ───────────────────────────────────────────────────────────────────────────

pi_extractor_cache = Table(
    "pi_extractor_cache", metadata,
    Column("char_id", Integer, primary_key=True, autoincrement=False),
    Column("planet_id", Integer, primary_key=True, autoincrement=False),
    Column("product_id", Integer, primary_key=True, autoincrement=False),
    Column("char_name", Text),
    Column("planet_name", Text),
    Column("product", Text),
    Column("expiry_iso", Text),
    Column("cached_at", Float),
)

planet_name_cache = Table(
    "planet_name_cache", metadata,
    Column("planet_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text),
)


# ───────────────────────────────────────────────────────────────────────────
# Margin tracker
# ───────────────────────────────────────────────────────────────────────────

margin_watchlist = Table(
    "margin_watchlist", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("type_id", Integer, nullable=False),
    Column("me", Integer, nullable=False, server_default="0"),
    Column("te", Integer, nullable=False, server_default="0"),
    Column("added_at", Float),
    # The same product at two ME levels is two genuinely different
    # propositions, so (type, ME, TE) is the identity, not the type.
    UniqueConstraint("type_id", "me", "te", name="uq_margin_watchlist_item"),
)

margin_snapshot = Table(
    "margin_snapshot", metadata,
    Column("item_id", Integer, primary_key=True, autoincrement=False),
    Column("day", Text, primary_key=True),          # YYYY-MM-DD, UTC
    Column("margin_pct", Float),
    Column("profit", Float),
    Column("sell_price", Float),
    Column("captured_at", Float),
)


# ───────────────────────────────────────────────────────────────────────────
# Production projects
# ───────────────────────────────────────────────────────────────────────────

production_projects = Table(
    "production_projects", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

project_plans = Table(
    "project_plans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Integer, nullable=False),
    Column("product_type_id", Integer, nullable=False),
    Column("product_name", Text, nullable=False),
    Column("quantity", Integer, nullable=False, server_default="1"),
    Column("me", Integer, server_default="0"),
    Column("te", Integer, server_default="0"),
    Column("station_name", Text),
    Column("facility_tax", Float, server_default="0"),
    Column("plan_json", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="'pending'"),
    Column("created_at", Float, nullable=False),
)

project_shopping = Table(
    "project_shopping", metadata,
    Column("project_id", Integer, primary_key=True, autoincrement=False),
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
    Column("needed", Integer, nullable=False, server_default="0"),
    Column("purchased", Integer, nullable=False, server_default="0"),
)

project_jobs = Table(
    "project_jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("plan_id", Integer, nullable=False),
    Column("project_id", Integer, nullable=False),
    Column("type_id", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("quantity", Integer, nullable=False, server_default="1"),
    Column("runs", Integer, nullable=False, server_default="1"),
    Column("step", Integer, nullable=False, server_default="1"),
    Column("activity", Text, nullable=False, server_default="'manufacturing'"),
    Column("status", Text, nullable=False, server_default="'pending'"),
)


# ───────────────────────────────────────────────────────────────────────────
# Public contract index
# ───────────────────────────────────────────────────────────────────────────
#
# ESI exposes no contract search, so a whole region is indexed once into these
# tables and searched locally.

public_contract_meta = Table(
    "public_contract_meta", metadata,
    Column("region_id", Integer, primary_key=True, autoincrement=False),
    Column("indexed_at", Float),
    Column("contract_count", Integer),
)

public_contracts = Table(
    "public_contracts", metadata,
    Column("contract_id", Integer, primary_key=True, autoincrement=False),
    Column("region_id", Integer),
    Column("type", Text),
    Column("price", Float),
    Column("reward", Float),
    Column("collateral", Float),
    Column("buyout", Float),
    Column("volume", Float),
    Column("date_expired", Text),
    Column("title", Text),
    Column("start_location_id", Integer),
    Column("end_location_id", Integer),
    Column("issuer_id", Integer),
    Index("idx_pc_region", "region_id"),
)

public_contract_items = Table(
    "public_contract_items", metadata,
    Column("contract_id", Integer),
    Column("type_id", Integer),
    Column("quantity", Integer),
    Column("is_included", Integer),
    Index("idx_pci_contract", "contract_id"),
    Index("idx_pci_type", "type_id"),
)


# ───────────────────────────────────────────────────────────────────────────
# Legacy Fuzzwork caches
# ───────────────────────────────────────────────────────────────────────────
#
# The only two tables that were ever managed by SQLAlchemy models
# (`app/db/database.py`). Declared here so one file describes the whole
# database; the models remain for the handful of call sites that use a
# Session.

type_cache = Table(
    "type_cache", metadata,
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
    Column("group_id", Integer),
    Column("category_id", Integer),
)

blueprint_cache = Table(
    "blueprint_cache", metadata,
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("blueprint_type_id", Integer),
    Column("data_json", Text, nullable=False),
    Column("cached_at", Float),
)


# ───────────────────────────────────────────────────────────────────────────
# Static Data Export — CCP's data, replaced wholesale, never migrated
# ───────────────────────────────────────────────────────────────────────────

sde_types = Table(
    "sde_types", metadata,
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
    Column("group_id", Integer),
    Column("published", Integer, server_default="1"),
    Column("market_group_id", Integer),
    # Volume of ONE unit as it ships, in m3. PACKAGED, not assembled: an
    # assembled Nidhoggur is 11,250,000 m3 and a packaged one is 1,300,000,
    # and it is the packaged figure that decides what a hauler carries and
    # therefore profit-per-m3. 829 types differ, all of them ships and
    # containers. The column this replaced was named `volume` and held the
    # assembled figure, which was wrong for exactly the items where it
    # mattered most.
    Column("packaged_volume", Float),
    # How many units one reprocessing batch consumes. Refining is
    # all-or-nothing per batch: 100 units for ore and compressed ore, 1 for
    # ice and batch-compressed ore. 75 Veldspar plus 25 Dense Veldspar is not
    # a batch — types cannot be combined. Also the output quantity of a
    # manufacturing run for the types that come out in stacks.
    Column("portion_size", Integer, server_default="1"),
    Index("idx_types_market_group", "market_group_id"),
)

sde_groups = Table(
    "sde_groups", metadata,
    Column("group_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
)

sde_blueprint_materials = Table(
    "sde_blueprint_materials", metadata,
    Column("blueprint_type_id", Integer, primary_key=True, autoincrement=False),
    Column("activity", Text, primary_key=True),     # manufacturing / reaction
    Column("material_type_id", Integer, primary_key=True, autoincrement=False),
    Column("quantity", Integer, nullable=False),
)

sde_blueprint_products = Table(
    "sde_blueprint_products", metadata,
    Column("blueprint_type_id", Integer, primary_key=True, autoincrement=False),
    Column("activity", Text, primary_key=True),
    Column("product_type_id", Integer, primary_key=True, autoincrement=False),
    Column("quantity", Integer, nullable=False),
    Column("probability", Float, server_default="1.0"),
    # "Which blueprint makes this item" — the single hottest SDE lookup there
    # is, and `product_type_id` is not a prefix of the primary key, so without
    # this it is a full scan of the table on every BOM node.
    Index("idx_bp_product", "product_type_id"),
)

sde_blueprints = Table(
    "sde_blueprints", metadata,
    Column("blueprint_type_id", Integer, primary_key=True, autoincrement=False),
    Column("max_production_limit", Integer, server_default="1"),
    Column("manufacturing_time", Integer, server_default="0"),
    Column("reaction_time", Integer, server_default="0"),
)

sde_blueprint_skills = Table(
    "sde_blueprint_skills", metadata,
    Column("blueprint_type_id", Integer, primary_key=True, autoincrement=False),
    Column("activity", Text, primary_key=True),
    Column("skill_type_id", Integer, primary_key=True, autoincrement=False),
    Column("required_level", Integer, nullable=False, server_default="1"),
)

sde_skill_time_bonus = Table(
    "sde_skill_time_bonus", metadata,
    Column("skill_type_id", Integer, primary_key=True, autoincrement=False),
    Column("skill_name", Text, nullable=False),
    Column("time_bonus_pct", Float, nullable=False),
)

sde_planet_schematics = Table(
    "sde_planet_schematics", metadata,
    Column("schematic_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text),
    Column("cycle_time", Integer),
    Column("output_type_id", Integer),
    Column("output_qty", Integer),
)

sde_planet_schematic_materials = Table(
    "sde_planet_schematic_materials", metadata,
    Column("schematic_id", Integer, primary_key=True, autoincrement=False),
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("quantity", Integer, nullable=False),
)

sde_decryptors = Table(
    "sde_decryptors", metadata,
    # Decryptors, from dogma attributes rather than a hardcoded table. There
    # are 64 of them, not the 8 every guide lists: each of the eight has
    # faction-flavoured duplicates (Cryptic/Esoteric/Incognito/Occult) and the
    # ancient-relic ones (Sleeper/Takmahl/Talocan/Yan Jung) used for reverse
    # engineering. A hardcoded table would cover an eighth of them and go
    # stale on the next rebalance.
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),
    Column("probability_mult", Float, nullable=False, server_default="1.0"),
    Column("me_modifier", Float, nullable=False, server_default="0.0"),
    Column("te_modifier", Float, nullable=False, server_default="0.0"),
    Column("run_modifier", Float, nullable=False, server_default="0.0"),
)

sde_type_materials = Table(
    "sde_type_materials", metadata,
    # What one BATCH of a type reprocesses into. The quantities are per
    # `sde_types.portion_size` units, not per unit, and they are the
    # 100%-yield figures before any skill, rig, structure or implant bonus —
    # a real refine never returns all of this.
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("material_type_id", Integer, primary_key=True, autoincrement=False),
    Column("quantity", Integer, nullable=False),
)

sde_market_groups = Table(
    "sde_market_groups", metadata,
    # The in-game market tree. `parent_group_id` is NULL for the dozen or so
    # roots; `has_types` marks the leaves that actually contain items, which
    # is what stops a browser offering empty branches.
    Column("market_group_id", Integer, primary_key=True, autoincrement=False),
    Column("parent_group_id", Integer),
    Column("name", Text, nullable=False),
    Column("has_types", Integer, nullable=False, server_default="0"),
    Column("icon_id", Integer),
    Index("idx_market_group_parent", "parent_group_id"),
)

sde_datacore_skills = Table(
    "sde_datacore_skills", metadata,
    # Which science skill each datacore is tied to, from its requiredSkill1
    # dogma attribute. Needed because invention's success formula counts only
    # the skills matching the datacores consumed, and the names do NOT match:
    # the skill is "Gallente Starship Engineering" while the datacore is
    # "Datacore - Gallentean Starship Engineering". Amarr/Amarrian differ the
    # same way. Matching on names silently drops one of the two science skills
    # for every Amarr and Gallente T2 ship.
    Column("type_id", Integer, primary_key=True, autoincrement=False),
    Column("skill_type_id", Integer, nullable=False),
)

sde_build = Table(
    "sde_build", metadata,
    # Which SDE build this database was built from. Without it the only answer
    # to "is this current?" is a row count, which cannot see a rebalance that
    # changes values without changing how many there are.
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("build_number", Integer, nullable=False),
    Column("release_date", Text),
    Column("imported_at", Float),
    CheckConstraint("id = 1", name="ck_sde_build_single_row"),
)


# ───────────────────────────────────────────────────────────────────────────
# Sync events
# ───────────────────────────────────────────────────────────────────────────
#
# The background sync worker (Step 4) refreshes caches. That alone is enough
# for the web UI, which re-reads the cache on every page load — but §9.5 wants
# a Discord bot that *announces* things, and a bot that polls a cache for
# changes is a bot that misses them: two changes between polls look like one,
# and a change that reverts looks like none. The doc is explicit that
# retrofitting event emission costs more than building it in, so the worker
# writes what changed as well as the new value.
#
# **Why an append-only table rather than a queue or a pub/sub.** The consumers
# are a second process (the bot) that has to survive restarts without missing
# anything, and eventually a web view. An in-process signal is lost on restart
# and invisible across processes; Postgres LISTEN/NOTIFY works for one backend
# and not the other, and this schema has to emit for both. A table with a
# monotonic id lets a consumer store a cursor and ask for everything after it —
# which is exactly "did I miss anything while I was down?". NOTIFY can be
# layered on later as a wake-up; the log stays the source of truth.
sync_events = Table(
    "sync_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Epoch seconds. BigInteger for the same reason as every other timestamp
    # here: Postgres INTEGER is four bytes and stops in 2038.
    Column("created_at", BigInteger, nullable=False),
    # Dotted, coarse-to-fine: "character.assets.changed", "job.completed".
    Column("kind", Text, nullable=False),
    # Whose event it is. Both nullable: a price refresh belongs to neither.
    Column("character_id", BigInteger),
    Column("corporation_id", BigInteger),
    # Small JSON. What changed, never the whole payload — a consumer that needs
    # the new value reads the cache, which is authoritative.
    Column("detail_json", Text),
    # A consumer asks for id > cursor, so this is the read path.
    Index("idx_sync_events_id_kind", "id", "kind"),
    Index("idx_sync_events_character", "character_id", "id"),
)


# ───────────────────────────────────────────────────────────────────────────
# Scopes
# ───────────────────────────────────────────────────────────────────────────

SDE_TABLES: frozenset[str] = frozenset(
    name for name in metadata.tables if name.startswith("sde_")
)
APP_TABLES: frozenset[str] = frozenset(metadata.tables) - SDE_TABLES

_SQLITE = sqlite_dialect.dialect()


def _ddl_for(names) -> list[str]:
    """CREATE statements for `names`, tables before their indexes."""
    tables = [metadata.tables[n] for n in sorted(names)]
    stmts = [
        str(CreateTable(t, if_not_exists=True).compile(dialect=_SQLITE)).strip()
        for t in tables
    ]
    stmts += [
        str(CreateIndex(ix, if_not_exists=True).compile(dialect=_SQLITE)).strip()
        for t in tables
        for ix in sorted(t.indexes, key=lambda i: i.name or "")
    ]
    return stmts


def _apply(conn: sqlite3.Connection, names) -> None:
    for stmt in _ddl_for(names):
        conn.execute(stmt)
    conn.commit()


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table this app writes, if absent. Idempotent.

    Takes a raw `sqlite3.Connection` because that is what the app passes
    around; the DDL is generated from the metadata rather than written by
    hand, so the same declaration will emit Postgres DDL when the engine
    changes.
    """
    _apply(conn, APP_TABLES)


# (database file, scope) pairs this process has already built. The code this
# replaces guarded the same work with a single process-wide boolean, which is
# wrong the moment a process touches two databases — which is every test run.
_APPLIED: set[tuple[str, str]] = set()


def _database_path(conn: sqlite3.Connection) -> str:
    """The file backing this connection's `main` schema, '' for in-memory."""
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file or ""
    return ""


def _ensure(conn: sqlite3.Connection, scope: str, names) -> None:
    """Apply a scope's DDL at most once per database per process.

    Creating forty tables costs around ten milliseconds, which is worth paying
    once at startup and not worth paying on every connection — the reason the
    DDL got scattered into lazy `ensure_*` calls in the first place.

    An in-memory database is never memoized: every `:memory:` connection is a
    distinct, empty database that merely shares a name.
    """
    path = _database_path(conn)
    if path and (path, scope) in _APPLIED:
        return
    _apply(conn, names)
    if path:
        _APPLIED.add((path, scope))


def ensure_schema(conn: sqlite3.Connection) -> None:
    """`apply_schema`, memoized. What runtime call sites should use."""
    _ensure(conn, "app", APP_TABLES)


def ensure_sde_schema(conn: sqlite3.Connection) -> None:
    """`apply_sde_schema`, memoized. What runtime call sites should use."""
    _ensure(conn, "sde", SDE_TABLES)


def forget_applied(path: str | None = None) -> None:
    """Drop the memo, for tests and for the SDE refresh that replaces the file."""
    if path is None:
        _APPLIED.clear()
    else:
        _APPLIED.difference_update({e for e in _APPLIED if e[0] == path})


def apply_sde_schema(conn: sqlite3.Connection) -> None:
    """Create the static-data tables and their indexes, if absent.

    Separate from `apply_schema` because these are replaced wholesale on every
    SDE build rather than migrated — and because the indexes here are the ones
    that keep disappearing: an SDE refresh drops each table and recreates it
    from its stored table DDL, which does not carry indexes with it.
    """
    _apply(conn, SDE_TABLES)


def create_sde_schema(bind) -> None:
    """Create the static-data tables on *any* backend. Idempotent.

    `apply_sde_schema` above takes a `sqlite3.Connection`, because that is what
    the importer and the test fixtures have always handed it. On Postgres there
    is no such object to pass, so on that backend there was simply **nothing
    that could create these tables** — which is the actual defect, and it is a
    duller one than it first looked.

    **The DDL itself was never the problem, and it is worth writing down that
    it was checked rather than assumed.** All fourteen SDE tables compile to
    byte-identical DDL on both dialects, and every SQLite-compiled statement
    runs on Postgres unaltered. The reason is `test_no_sde_table_mints_its_own_id`
    over in `tests/test_sde_on_postgres.py`: every SDE primary key is a natural
    key CCP assigned, so nothing here is a `SERIAL`, and `SERIAL` versus
    `INTEGER PRIMARY KEY` is where six of the *app* tables do diverge.

    So `create_all` is not buying a translation today. It is buying the
    guarantee that it keeps compiling for whatever dialect is underneath if
    that stops being true — the first SDE table to want a generated id would
    otherwise be a silent divergence, not an error.

    **Why the SDE is not in the Alembic history**, given that the app tables
    are: these are replaced wholesale on every SDE build and carry no user
    data, so there is nothing to migrate *from*. Versioning them would mean
    writing a migration every time CCP adds a column, to move data that is
    about to be overwritten anyway. `app/db/migrate.py` excludes them from
    autogenerate for the same reason. The consequence is that something else
    has to create them, and until now nothing did on Postgres — six statements
    JOIN `sde_types` to runtime tables, so the app could not serve a page.

    `checkfirst=True` is the default and is what makes this idempotent; it also
    means an existing table is left exactly as it is rather than being altered
    to match, so a *changed* SDE table shape still needs the drop-and-rebuild
    that `import_sde.py --fresh` does.
    """
    tables = [metadata.tables[n] for n in sorted(SDE_TABLES)]
    metadata.create_all(bind, tables=tables, checkfirst=True)


def upsert(table: str, columns, update=None) -> str:
    """`INSERT ... ON CONFLICT (pk) DO UPDATE SET ...` for one table.

    Replaces `INSERT OR REPLACE`, which was both SQLite-only and quietly
    destructive. The two are not equivalent: `OR REPLACE` deletes the existing
    row and inserts a new one, so **every column the statement does not name is
    reset to NULL**. Eight call sites were doing that:

    * `station_rigs` — saving an ME bonus wiped the structure type and all
      three rig slots, so adjusting a number un-configured the station.
    * `location_name_cache` — five writers reset `region_id`, which is filled
      in by two ESI calls in `get_region_for_location()` and then thrown away
      by the next asset refresh.
    * `sde_types` — caching an ESI-resolved name nulled `packaged_volume`,
      the figure that decides what a hauler carries. That column exists
      because using the assembled volume instead was a real bug once already.

    `ON CONFLICT DO UPDATE` writes only what it is given, so the class of bug
    goes away with the dialect problem. The conflict target is read from the
    declared primary key rather than repeated at the call site — there is
    already one source of truth for that.

    Placeholders are `?`. When the store moves to Postgres this is the single
    function that has to start emitting `%s`.
    """
    t = metadata.tables[table]
    pk = [c.name for c in t.primary_key.columns]
    if not pk:
        raise ValueError(f"{table} has no primary key, so it cannot upsert")
    cols = list(columns)
    missing = set(pk) - set(cols)
    if missing:
        raise ValueError(
            f"{table} upsert must supply its whole key; missing {sorted(missing)}")

    assignments = list(update) if update is not None else [c for c in cols if c not in pk]
    placeholders = ",".join("?" * len(cols))
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT ({', '.join(pk)}) DO ")
    if not assignments:
        # Key-only table: the row's existence is the whole of its content.
        return sql + "NOTHING"
    return sql + "UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in assignments)


def sde_index_ddl(tables=None) -> list[str]:
    """The SDE index statements, for `tables` (default: all of them).

    `_refresh_sde_from_bundle()` drops and recreates each SDE table, and
    dropping a table drops its indexes, so it replays these afterwards. It
    passes the tables it actually rebuilt: a database that has never held the
    full SDE — a partial refresh, or a test fixture with two tables in it —
    would otherwise be asked to index a table that is not there.
    """
    names = SDE_TABLES if tables is None else (set(tables) & SDE_TABLES)
    return [
        str(CreateIndex(ix, if_not_exists=True).compile(dialect=_SQLITE)).strip()
        for name in sorted(names)
        for ix in sorted(metadata.tables[name].indexes, key=lambda i: i.name or "")
    ]
