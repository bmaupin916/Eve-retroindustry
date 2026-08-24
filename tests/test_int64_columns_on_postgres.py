"""Columns that hold real EVE ids and market volumes must survive real values.

SQLite's INTEGER is a variable-width 64-bit type and treats the declaration as
advisory, so a column declared `Integer` stores 1.03e12 there without complaint.
A Postgres INTEGER is exactly 32 bits and raises `NumericValueOutOfRange`. That
asymmetry is why every one of these columns was wrong for the life of the
project and nothing noticed: the only backend anyone ran did not care.

`station_rigs.location_id` was the first to be caught, in v0.9.56, and only
because a converted module finally got exercised against Postgres.

**ESI's declared type does not decide which columns need widening.** CCP
declares essentially every integer field as int64 — `type_id`, `group_id` and
`category_id` included — so taking the declaration literally means widening
everything, which is the reflex this file exists to avoid. What decides it is
the range the values actually occupy, measured:

    type_id             371,027   0.017% of the int32 ceiling
    solar_system_id  30,030,141   1.4%
    contract_id     234,465,667   10.9%
    corporation_id  2,042,491,468  95.1%     <- widened
    character_id    2,124,549,094  98.9%     <- widened
    order_id        7,407,646,135  3.4x over <- no column holds one
    location_id     1,049,982,731,184  489x over  <- widened
    volume (7d, Tritanium) 34,190,149,437  15.9x over <- widened

The character ids are the uncomfortable ones. 98.9% of the ceiling is not a
future problem: CCP mints ids continuously, and the headroom is about 23
million.

The volume columns were not on the original list, because the original list was
about ids. `market_price_cache.volume` is seven days of regional trade and
`jita_available` is every Jita sell order's remaining units added together —
both measured above the ceiling today for the common minerals, which is most of
what an industry tool prices.

Each parameterisation writes a realistic oversized value and reads it back. On
SQLite they all pass with or without the migration, which is the whole point:
only the Postgres half can fail, and before migration 0010 every one of them did.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_int64"

INT32_MAX = 2_147_483_647

# A real Upwell structure id, from a public contract's start_location_id.
STRUCTURE_ID = 1_049_982_731_184
# Seven days of Tritanium traded in The Forge.
BIG_VOLUME = 34_190_149_437

# The highest character id actually seen when this was written, sampled from
# public contract issuers: 2,124,549,094, or 98.9% of the int32 ceiling.
HIGHEST_CHARACTER_ID_SEEN = 2_124_549_094

# ...and the value the tests actually write, which is deliberately *not* that
# one. Today's ids still fit in 32 bits, so writing a real one passes with or
# without the fix and proves nothing about it. CCP mints ids sequentially and
# the remaining headroom is about 23 million, so this is the id of a character
# created a little way into the future — near enough that "it fits for now" is
# not a reason to leave the column narrow, and far enough that the assertion can
# actually fail. These are the only cases here that are preventive rather than
# a fix for something already broken.
CHARACTER_ID = 2_200_000_000

# (table, column, value, the other columns needed to make the row legal)
CASES = [
    # ── character ids, one per table that stores one ─────────────────────────
    ("app_bootstrap", "character_id", CHARACTER_ID,
     {"token": "t", "created_at": 0.0}),
    ("app_owner", "character_id", CHARACTER_ID,
     {"id": 1, "claimed_at": 0.0}),
    ("app_sessions", "character_id", CHARACTER_ID,
     {"session_id": "s", "csrf_token": "c", "created_at": 0.0,
      "last_seen_at": 0.0}),
    ("char_assets_cache", "character_id", CHARACTER_ID, {"data_json": "[]"}),
    ("char_blueprints_cache", "character_id", CHARACTER_ID, {"data_json": "[]"}),
    ("char_skills_cache", "character_id", CHARACTER_ID,
     {"data_json": "[]", "cached_at": 0.0}),
    ("char_wallet_cache", "character_id", CHARACTER_ID,
     {"balance": 0.0, "cached_at": 0.0}),
    ("characters", "character_id", CHARACTER_ID,
     {"character_name": "n", "refresh_token": "r", "added_at": 0.0}),
    ("pi_extractor_cache", "char_id", CHARACTER_ID,
     {"planet_id": 40_000_001, "product_id": 2268}),

    # ── corporation ids ──────────────────────────────────────────────────────
    ("characters", "corporation_id", CHARACTER_ID,
     {"character_id": 1, "character_name": "n", "refresh_token": "r",
      "added_at": 0.0}),
    ("corp_assets_cache", "corporation_id", CHARACTER_ID, {"data_json": "[]"}),

    # ── structure ids ────────────────────────────────────────────────────────
    ("location_name_cache", "location_id", STRUCTURE_ID, {"name": "n"}),
    ("station_volume_cache", "location_id", STRUCTURE_ID, {"type_id": 34}),
    ("facility_tax_cache", "facility_id", STRUCTURE_ID,
     {"tax_rate": 0.01, "cached_at": 0}),
    ("public_contracts", "start_location_id", STRUCTURE_ID, {"contract_id": 1}),
    ("public_contracts", "end_location_id", STRUCTURE_ID, {"contract_id": 2}),
    ("public_contracts", "issuer_id", CHARACTER_ID, {"contract_id": 3}),

    # ── market volumes ───────────────────────────────────────────────────────
    ("market_price_cache", "volume", BIG_VOLUME, {"type_id": 34}),
    ("market_price_cache", "jita_available", 12_564_293_700, {"type_id": 35}),
    ("hub_price_cache", "volume", BIG_VOLUME,
     {"region_id": 10000002, "type_id": 34}),
    ("hub_price_cache", "available", 12_564_293_700,
     {"region_id": 10000002, "type_id": 35}),
    ("station_volume_cache", "volume", BIG_VOLUME,
     {"location_id": 60003760, "type_id": 34}),
    ("station_volume_cache", "traded_volume", BIG_VOLUME,
     {"location_id": 60003760, "type_id": 35}),
]


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'int64.db'}")
        from app.db.migrate import upgrade_to_head
        upgrade_to_head(f"sqlite:///{tmp_path / 'int64.db'}")
        yield eng
        eng.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

    from app.db.migrate import upgrade_to_head

    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.commit()
    admin.dispose()

    scoped = PG_URL + ("&" if "?" in PG_URL else "?") + \
        f"options=-csearch_path%3D{PG_SCHEMA}"
    upgrade_to_head(scoped)

    eng = create_engine(scoped)
    yield eng
    eng.dispose()


# One readable id per case, so a failure names the column rather than "case 14".
CASE_IDS = [f"{table}.{column}" for table, column, _v, _o in CASES]


@pytest.mark.parametrize("table,column,value,others", CASES, ids=CASE_IDS)
def test_a_real_sized_value_round_trips(engine, table, column, value, others):
    """Write the value a real EVE install produces, then read it back.

    Reading it back matters as much as writing it: a narrowing that silently
    truncated rather than raising would pass a write-only assertion.
    """
    cols = {column: value, **others}
    names = ", ".join(cols)
    binds = ", ".join(f":{c}" for c in cols)

    with engine.connect() as c:
        c.execute(text(f"INSERT INTO {table} ({names}) VALUES ({binds})"), cols)
        c.commit()

    with engine.connect() as c:
        where = " AND ".join(f"{k} = :{k}" for k in others) or "1=1"
        got = c.execute(
            text(f"SELECT {column} FROM {table} WHERE {where}"), others).fetchone()

    assert got is not None, f"on {engine.dialect.name}: the row vanished"
    assert got[0] == value, (
        f"on {engine.dialect.name}: {table}.{column} came back as {got[0]}, "
        f"not {value} — the column truncated instead of storing it")


def test_the_case_list_covers_every_value_that_needs_it(engine):
    """A guard on the list itself.

    Every case here is meant to exceed what a 32-bit column can hold, or to sit
    close enough to the ceiling that it will. A case that quietly fits proves
    nothing on either backend and would sit in the file looking like coverage.
    """
    for table, column, value, _ in CASES:
        assert value > INT32_MAX * 0.9, (
            f"{table}.{column} uses {value:,}, which is only "
            f"{value / INT32_MAX * 100:.1f}% of the int32 ceiling — too small "
            f"to prove anything")
