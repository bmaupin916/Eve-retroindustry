"""The PI unit's converted statements, executed against both backends.

`tests/test_pi_planner.py` exercises this code through the app, and the app is
bound to SQLite for the whole test session — so every assertion there is a
SQLite assertion. That is the gap this file closes: the PI unit was converted
**for** Postgres and nothing was running its SQL there.

Why the statements are copied rather than called: `_store_pi_cache_for_chars`,
`_pi_alert_summary` and the three `pi_planner_helper` lookups open their own
connection through `app.db.conn.connect()`, which resolves the *configured*
database. There is no seam to point them at a second backend without setting
`EVE_DATABASE_URL` for the process, and doing that inside a test would rebind
the app for every other test in the session.

So this is a **dialect test, not a behaviour test**, and it is worth being plain
about the difference. It proves each statement parses, binds and runs on
Postgres — which is where a conversion fails, since `?`-vs-`:name`, expanding
`IN`, `ON CONFLICT` and `sqlite_master` are all dialect questions. What the
statements *mean* is pinned by `test_pi_planner.py`. Neither file is sufficient
alone.

The `has_table` probe is here for a specific reason: it replaced
`SELECT name FROM sqlite_master WHERE type='table' AND name=…`, which was the
last use of SQLite's catalog table in the app and simply does not exist on
Postgres. A test that only ever ran on SQLite could not have told you that.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import bindparam, create_engine, inspect as sa_inspect, text

from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_pi_sql"

CHAR_A, CHAR_B = 900000001, 900000002
PLANET_A, PLANET_B = 4001, 4002
AQUEOUS = 2268


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    from app.db.migrate import upgrade_to_head

    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'pi.db'}"
        upgrade_to_head(url)
        eng = create_engine(url)
        yield eng
        eng.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

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


@pytest.fixture
def conn(engine):
    """A connection with an empty extractor cache. Emptied before, not after, so
    a test that dies half-way leaves nothing for the next one."""
    with engine.connect() as c:
        c.execute(text("DELETE FROM pi_extractor_cache"))
        c.commit()
        yield c


def _row(char_id: int, planet_id: int, expiry: str, *, product: int = AQUEOUS,
         name: str = "Pilot", planet: str = "Testworld") -> dict:
    return {"char_id": char_id, "char_name": name, "planet_id": planet_id,
            "planet_name": planet, "product_id": product, "product": "Aqueous Liquids",
            "expiry_iso": expiry, "cached_at": time.time()}


UPSERT = text(
    "INSERT INTO pi_extractor_cache"
    " (char_id, char_name, planet_id, planet_name,"
    "  product_id, product, expiry_iso, cached_at)"
    " VALUES (:char_id, :char_name, :planet_id, :planet_name,"
    "  :product_id, :product, :expiry_iso, :cached_at)"
    " ON CONFLICT (char_id, planet_id, product_id) DO UPDATE SET"
    " char_name=excluded.char_name, planet_name=excluded.planet_name,"
    " product=excluded.product, expiry_iso=excluded.expiry_iso,"
    " cached_at=excluded.cached_at")

DELETE_FOR_CHARS = (
    text("DELETE FROM pi_extractor_cache WHERE char_id IN :cids")
    .bindparams(bindparam("cids", expanding=True)))


def test_the_extractor_upsert_runs_on_both_backends(conn):
    """`ON CONFLICT (…) DO UPDATE SET … excluded.…` is Postgres syntax that
    SQLite adopted, so it survives the move — asserted rather than assumed,
    because it is the single most common statement shape in this codebase."""
    conn.execute(UPSERT, [_row(CHAR_A, PLANET_A, "2026-01-01T00:00:00Z")])
    conn.commit()

    conn.execute(UPSERT, [_row(CHAR_A, PLANET_A, "2026-06-01T00:00:00Z")])
    conn.commit()

    rows = conn.execute(text(
        "SELECT char_id, planet_id, expiry_iso FROM pi_extractor_cache")).fetchall()
    assert len(rows) == 1, f"the conflict inserted instead of updating: {rows}"
    assert rows[0][2] == "2026-06-01T00:00:00Z"


def test_a_batch_that_collides_with_itself_keeps_the_last_row(conn):
    """The only way the `DO UPDATE` is reachable in production: two extractor
    pins on one planet pulling the same P0, in a single executemany.

    Worth running on both backends specifically because "the last row wins" is
    an ordering claim, and the two drivers apply a multi-row execute
    differently — psycopg sends one statement per row, sqlite3 loops."""
    conn.execute(UPSERT, [
        _row(CHAR_A, PLANET_A, "2026-01-01T00:00:00Z"),
        _row(CHAR_A, PLANET_A, "2026-09-09T00:00:00Z"),
    ])
    conn.commit()

    rows = conn.execute(text(
        "SELECT expiry_iso FROM pi_extractor_cache")).fetchall()
    assert len(rows) == 1, f"a self-colliding batch left {len(rows)} rows"
    assert rows[0][0] == "2026-09-09T00:00:00Z", (
        "the later row in the batch did not win")


def test_the_per_character_delete_expands_on_both_backends(conn):
    """The expanding `IN` — the construct that has no direct `?`-tuple
    equivalent, and the one most likely to render differently per dialect.

    Two characters, so "deleted the right one" and "deleted everything" are
    different observations.
    """
    conn.execute(UPSERT, [_row(CHAR_A, PLANET_A, "2026-01-01T00:00:00Z"),
                          _row(CHAR_B, PLANET_B, "2026-01-01T00:00:00Z")])
    conn.commit()

    conn.execute(DELETE_FOR_CHARS, {"cids": [CHAR_A]})
    conn.commit()

    remaining = [r[0] for r in conn.execute(text(
        "SELECT char_id FROM pi_extractor_cache")).fetchall()]
    assert remaining == [CHAR_B], (
        f"the scoped delete removed the wrong rows: {remaining}")


def test_max_cached_at_on_an_empty_table_is_a_row_holding_none(conn):
    """`SELECT MAX(cached_at)` decides whether the dashboard tile rebuilds.

    An aggregate over no rows returns **one row containing NULL**, not zero
    rows — on both backends — which is why the caller reads
    `_age_row[0]` and tests it rather than testing `_age_row`. Pinned because
    the opposite assumption produces an `IndexError` that only shows up on a
    database nobody has visited yet.
    """
    row = conn.execute(text("SELECT MAX(cached_at) FROM pi_extractor_cache")).fetchone()

    assert row is not None, "an aggregate returned no row at all"
    assert row[0] is None


def test_has_table_answers_on_both_backends(conn):
    """What replaced `SELECT name FROM sqlite_master`.

    The old probe could not run on Postgres at all — the catalog table does not
    exist there — so `/planets` would have raised on every request, from a
    table-existence check rather than from any query it guarded. The inspector
    answers the same question in whichever dialect it is bound to, and answers
    it correctly in both directions.
    """
    inspector = sa_inspect(conn)

    assert inspector.has_table("pi_extractor_cache") is True
    assert inspector.has_table("a_table_that_does_not_exist") is False, (
        "the probe says yes to everything, so guarding on it means nothing")
