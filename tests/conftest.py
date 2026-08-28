"""Test fixtures: a synthetic app instance backed by the bundled SDE.

The DB is built from the committed ``sde_base.db`` (available in CI) plus a
little synthetic user data, plus stubs for the live ESI fetchers, so a test run
needs no network.

**The environment is set up at import, not in a fixture.** ``EVE_APP_DIR`` used
to be set inside ``app_module``, which runs after collection — and collection
imports every test module. The moment one of them imported an ``app.web``
module at module level, the app computed the database path from the unset
variable, bound the developer's real ``eve_cache.db``, and the whole run wrote
there: ``_seed`` below opens with ``DELETE FROM characters``.

That is not a hypothetical. It happened, it took real characters and their
refresh tokens with it, and nothing failed — the suite went green for a while
and then started failing in unrelated files, because it was reading somebody's
actual data. The docstring that used to claim "tests never touch the real
eve_cache.db" was a claim about the world with nothing asserting it.

The app no longer freezes that path at all (``app/db/location.py``), which
makes the failure impossible rather than merely guarded. Setting the variable
early is still right — a subprocess or a module that reads it once should see
the test value — and ``pytest_collection_finish`` below still checks the
outcome, because a guard whose premise has been fixed is cheap to keep and
expensive to have removed on the day the premise comes back.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NPC_STATION = 60003760           # Jita IV-4 (resolves from the SDE, no ESI)
JITA_SYSTEM = 30000142

REAL_DB = os.path.join(REPO, "eve_cache.db")

# Import time, before pytest collects (and therefore imports) any test module.
#
# Idempotent, because this file gets imported twice: pytest loads it as
# `conftest`, and `tests/test_auth_gate.py` imports it again as
# `tests.conftest`. Two module objects, two executions of everything at module
# level — so without the marker the second one mints a second temp directory,
# repoints EVE_APP_DIR at it, and leaves the already-imported app bound to the
# first. Same failure shape as binding the real database, one step removed.
_MARKER = "EVE_TEST_APP_DIR"
APP_DIR = os.environ.get(_MARKER) or ""
if not APP_DIR or not os.path.isdir(APP_DIR):
    APP_DIR = tempfile.mkdtemp(prefix="eve-test-")
    shutil.copy2(os.path.join(REPO, "sde_base.db"),
                 os.path.join(APP_DIR, "eve_cache.db"))
    os.environ[_MARKER] = APP_DIR
TEST_DB = os.path.join(APP_DIR, "eve_cache.db")
os.environ["EVE_APP_DIR"] = APP_DIR
os.environ["EVE_BUNDLE_DIR"] = REPO
# TestClient sends "Host: testserver"; the security gate rejects unknown hosts,
# which is the point of it. Tests opt that name in rather than switching the
# check off, so the Host path stays exercised.
os.environ["EVE_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ.pop("EVE_OWNER_CHARACTER_ID", None)
# The background sync worker is default-on, because a deployment without it is a
# set of caches nobody refreshes. A test run is the other case entirely: every
# `with TestClient(app)` would start a loop that fetches from live ESI.
os.environ["EVE_SYNC_WORKER"] = "0"
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def pytest_collection_finish(session):
    """Last moment before anything runs: prove the app is not bound to the real DB.

    Collection has imported every test module by now, so if any of them pulled
    in an app module too early this is where it shows — before a single test
    has had the chance to write.
    """
    from app.db.location import app_dir, database_path

    wrong = []
    if os.path.abspath(database_path()) != os.path.abspath(TEST_DB):
        wrong.append(f"database_path() = {os.path.abspath(database_path())}")
    if os.path.abspath(app_dir()) != os.path.abspath(APP_DIR):
        wrong.append(f"app_dir() = {os.path.abspath(app_dir())}")
    if wrong:
        detail = "\n  ".join(wrong)
        raise pytest.UsageError(
            "refusing to run: the app is bound to a database that is not the "
            f"test one ({TEST_DB}).\n  {detail}\n"
            "A test module imported an app module before this conftest set "
            "EVE_APP_DIR. The suite opens with DELETE FROM characters."
        )


def _seed(m) -> None:
    conn = m.get_conn()          # ensures the user tables exist
    try:
        now = time.time()
        rows = conn.execute(
            "SELECT type_id, name FROM sde_types WHERE type_id IN (34,35,36,37,38)"
        ).fetchall()
        names = {r[0]: r[1] for r in rows}
        ids = sorted(names) or [34]
        chars = [(900000001, "Test Pilot Alpha", 98000001),
                 (900000002, "Test Pilot Beta", 98000001)]
        conn.execute("DELETE FROM characters")
        for i, (cid, nm, corp) in enumerate(chars):
            conn.execute(
                "INSERT INTO characters (character_id, character_name, refresh_token, "
                "access_token, token_expires_at, corporation_id, last_sync_at, added_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, nm, "test", "test", now + 10**9, corp, now, now + i))
            conn.execute(
                "INSERT OR REPLACE INTO char_wallet_cache (character_id, balance, cached_at) "
                "VALUES (?,?,?)", (cid, 1_000_000_000.0 * (i + 1), now))
            assets = [{"item_id": 1000 + j, "type_id": t, "quantity": 10_000,
                       "location_id": NPC_STATION, "location_flag": "Hangar",
                       "is_singleton": False, "name": names.get(t, str(t)),
                       "solar_system_id": JITA_SYSTEM} for j, t in enumerate(ids)]
            conn.execute(
                "INSERT OR REPLACE INTO char_assets_cache (character_id, data_json, cached_at) "
                "VALUES (?,?,?)", (cid, json.dumps(assets), now))
            conn.execute(
                "INSERT OR REPLACE INTO char_blueprints_cache (character_id, data_json, cached_at) "
                "VALUES (?,?,?)", (cid, json.dumps([]), now))
        for t in ids:
            conn.execute(
                "INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) "
                "VALUES (?,?,?,?)", (t, 5.0, 4.0, now))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def app_module():
    import app.web.main as m
    from app.db.location import database_path

    # Belt and braces with pytest_collection_finish, which runs before any test
    # and cannot see a variable something changes afterwards.
    assert os.path.abspath(database_path()) == os.path.abspath(TEST_DB), (
        f"the app is bound to {database_path()}, not {TEST_DB}"
    )
    m._SDE_READY[0] = True
    _seed(m)

    # Stub live-only ESI fetchers so rendering is hermetic and fast.
    async def _loc(client, cid, tok):
        return {"station_id": NPC_STATION}

    async def _sq(client, cid, tok):
        return []

    m.fetch_location = _loc
    m.fetch_skill_queue = _sq

    yield m
    shutil.rmtree(APP_DIR, ignore_errors=True)


# The character conftest logs in as; must match a row created by _seed().
OWNER_CHARACTER_ID = 900000001


def _login(m) -> tuple[str, str]:
    """Mint a real session for the seeded owner. Returns (session_id, csrf_token).

    Tests authenticate the way the app does rather than bypassing the gate, so
    the session and CSRF paths are covered by every route test instead of being
    a hole nothing walks through.
    """
    from app.web import security

    # `security` is on the portable query layer, so this is an engine
    # connection. Every route test in the suite depends on the session this
    # mints, which is why it is worth saying plainly rather than leaving the
    # reader to infer it from a fixture two files away.
    from app.db.conn import connect

    with connect() as conn:
        security.ensure_sessions_table(conn)
        security.claim_owner(conn, OWNER_CHARACTER_ID)
        return security.create_session(conn, OWNER_CHARACTER_ID)


@pytest.fixture(scope="session")
def client(app_module):
    from fastapi.testclient import TestClient
    from app.web import security

    session_id, csrf_token = _login(app_module)
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)
    c.headers.update({security.CSRF_HEADER: csrf_token})
    return c


@pytest.fixture(scope="session")
def anon_client(app_module):
    """A client with no session — for asserting the gate actually refuses."""
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


# ── Postgres test schemas ───────────────────────────────────────────────────
# Every `test_*_on_postgres.py` builds its own `pytest_*` schema and drops it
# at *setup* rather than teardown, so a container accumulates one per module
# and keeps it until that module runs again. Twenty-four were sitting in the
# dev container when this was written.
#
# Sweeping at session end is the normal path. The setup-time drop stays where
# it is: it is what makes the leak self-healing after a SIGKILL, which no
# teardown can cover.


#: Schemas this suite creates. The escape matters: in a LIKE pattern `_` is
#: a single-character wildcard, so an unescaped `pytest_%` also matches, say,
#: `pytestXfoo`. Nothing is named that today, which is exactly why it would
#: not be noticed.
TEST_SCHEMA_PATTERN = "pytest\\_%"


def _matching_schemas(conn, pattern: str = TEST_SCHEMA_PATTERN) -> list[str]:
    """Schema names the sweep would drop. Separated from the dropping so a
    test can assert what the selector matches without destroying schemas a
    concurrent module-scoped fixture is still using."""
    from sqlalchemy import text

    rows = conn.execute(
        text("SELECT nspname FROM pg_namespace WHERE nspname LIKE :p ORDER BY nspname"),
        {"p": pattern},
    ).fetchall()
    return [r[0] for r in rows]


def _sweep_postgres_test_schemas(pattern: str = TEST_SCHEMA_PATTERN) -> list[str]:
    """Drop every schema `pattern` matches. Returns the names actually dropped.

    A concurrent run holds its own schema and `DROP ... CASCADE` blocks on it
    rather than failing, so this sets a short `lock_timeout` and moves on —
    that schema has a live owner who will drop it.
    """
    try:
        from sqlalchemy import create_engine, text

        from tests.test_postgres_schema import URL, _reachable
    except Exception:                       # noqa: BLE001 — no sqlalchemy, no sweep
        return []
    if not _reachable(URL):
        return []

    dropped: list[str] = []
    engine = create_engine(URL)
    try:
        with engine.connect() as conn:
            for name in _matching_schemas(conn, pattern):
                try:
                    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                    conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
                    conn.commit()
                    dropped.append(name)
                except Exception:           # noqa: BLE001 — in use by a live run
                    conn.rollback()
    finally:
        engine.dispose()
    return dropped


def pytest_sessionfinish(session, exitstatus):
    """Leave the container as clean as the run found it."""
    _sweep_postgres_test_schemas()
