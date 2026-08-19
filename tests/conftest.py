"""Test fixtures: a synthetic app instance backed by the bundled SDE.

The DB is built from the committed ``sde_base.db`` (available in CI) plus a
little synthetic user data, plus stubs for the live ESI fetchers, so a test run
needs no network.

**The environment is set up at import, not in a fixture.** ``EVE_APP_DIR`` used
to be set inside ``app_module``, which runs after collection — and collection
imports every test module. The moment one of them imported an ``app.web``
module at module level, ``app.web.deps`` computed ``DB_ABS`` from the unset
variable, bound it to the developer's real ``eve_cache.db``, and the whole run
wrote there: ``_seed`` below opens with ``DELETE FROM characters``.

That is not a hypothetical. It happened, it took real characters and their
refresh tokens with it, and nothing failed — the suite went green for a while
and then started failing in unrelated files, because it was reading somebody's
actual data. ``pytest_collection_finish`` below is the net; the docstring that
used to claim "tests never touch the real eve_cache.db" was a claim about the
world with nothing asserting it.
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
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def pytest_collection_finish(session):
    """Last moment before anything runs: prove the app is not bound to the real DB.

    Collection has imported every test module by now, so if any of them pulled
    in an app module too early this is where it shows — before a single test
    has had the chance to write.
    """
    # Every module that freezes a filesystem path at import. `app.db.location`
    # is on the list because `tests/test_db_conn.py` imports it at module level,
    # which is how this first went wrong.
    frozen = [("app.web.deps", "DB_ABS"), ("app.db.location", "DB_PATH")]
    wrong = []
    for mod_name, attr in frozen:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue                    # nothing imported it; nothing to bind
        bound = os.path.abspath(getattr(mod, attr))
        if bound != os.path.abspath(TEST_DB):
            wrong.append(f"{mod_name}.{attr} = {bound}")
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
    from app.web import deps

    # Belt and braces with pytest_collection_finish: that hook only fires if
    # something imported deps during collection, and this one covers the case
    # where main is imported here for the first time.
    assert os.path.abspath(deps.DB_ABS) == os.path.abspath(TEST_DB), (
        f"the app is bound to {deps.DB_ABS}, not {TEST_DB}"
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

    conn = m.get_conn()
    try:
        security.ensure_sessions_table(conn)
        security.claim_owner(conn, OWNER_CHARACTER_ID)
        return security.create_session(conn, OWNER_CHARACTER_ID)
    finally:
        conn.close()


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
