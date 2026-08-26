"""`token_store`, `character.jobs` and `character.skills`, before they convert.

These three are what the last `dbapi()` boundary points at — `list_characters`,
`load_cached_jobs` and `get_cached_skills`, all reached through the one `raw =
dbapi(conn)` left in `routers/industry.py`. Converting them closes the boundary
count at zero.

The coverage probe over `app/character/*` plus `token_store` says **35 of 102
functions are never executed by the suite**. Fifteen of those are in these three
modules, and the probe is not lying about the fetchers: the worker tests
monkeypatch `fetch_assets` and friends onto the worker module, so the real ones
genuinely never run.

Two things here are riskier than anything the previous slices touched:

* `token_store` owns the `characters` table, which holds refresh tokens. The
  suite once wrote to the real database and cost three characters and their
  tokens. `config_path()` resolves per call from `EVE_APP_DIR`, and
  `tests/conftest.py` sets that at import, which is what makes the JSON-migration
  tests here safe to write at all.
* `save_cached_jobs` does **not** commit. Its only caller, `fetch_industry_jobs`,
  commits for it. That split is pinned below rather than assumed, because a
  conversion that "helpfully" adds a commit inside the writer changes where the
  transaction boundary is, and one that drops the caller's commit loses the
  write silently.

**These assertions are unchanged by the conversion.** They were written
against the `sqlite3` versions first, exactly so they could be preserved rather
than invented afterwards to fit whatever the rewrite did. Only the fixture
underneath moved, and it now runs each of them on both backends.

Postgres comes from the container in `tests/test_postgres_schema.py`; without it
those parameterisations skip and the SQLite half still runs.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from sqlalchemy import create_engine, text

from app.auth import token_store as ts
from app.character import jobs as jobs_api
from app.character import skills as skills_api
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_character_caches"

CHAR = 2_112_625_428
CORP = 98_000_001


# ── stubs ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *responses):
        self._queue = list(responses)
        self.urls: list[str] = []

    async def get(self, url, **kw):
        self.urls.append(url)
        nxt = self._queue.pop(0) if self._queue else _Resp(500)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def calls(self) -> int:
        return len(self.urls)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    """An engine per backend, with the app tables present and empty.

    A file rather than `:memory:` on SQLite, because the commit assertions open
    a *second* connection and every `:memory:` connection is a distinct, empty
    database that merely shares a name.
    """
    from app.db.migrate import upgrade_to_head

    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp("db") / "eve_cache.db"}"
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



#: Emptied before every test, so one module-scoped schema can serve them all.
_CLEARED = ("characters", "char_skills_cache", "char_jobs_cache",)


@pytest.fixture(autouse=True)
def _empty_tables(engine):
    """Per-test isolation without a per-test schema rebuild.

    The engine is module-scoped now — building it runs every migration, which
    at function scope cost more than the tests themselves. These tests only ever
    assert on rows they insert, so emptying the tables gives the same isolation.

    Before, not after: a test that dies half-way must not leave its rows for the
    next one to read.
    """
    with engine.connect() as c:
        for table in _CLEARED:
            c.execute(text(f"DELETE FROM {table}"))
        c.commit()
    yield

@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _backend(conn) -> str:
    return conn.engine.dialect.name


@pytest.fixture(autouse=True)
def _restore_config_file():
    """`.eve_config.json` lives in the shared test app dir, not in `tmp_path`.

    The migration tests write it and one of them deliberately leaves a
    `client_id` behind, so without this they would leak state into whatever runs
    next — the same class of problem as the module globals in
    `test_location_resolver_on_postgres.py`, and a worse one here because
    `get_client_id()` reads this file.
    """
    import os
    path = ts.config_path()
    had = os.path.exists(path)
    before = open(path, "rb").read() if had else None
    yield
    if had:
        with open(path, "wb") as f:
            f.write(before)
    elif os.path.exists(path):
        os.remove(path)


def _add_character(conn, char_id=CHAR, name="Astroasia", refresh="r-token"):
    conn.execute(
        text("INSERT INTO characters (character_id, character_name,"
             " refresh_token, added_at) VALUES (:cid, :name, :refresh, 1000.0)"),
        {"cid": char_id, "name": name, "refresh": refresh})
    conn.commit()


# ── the schema shims ─────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing file: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    assert conn.execute(
        text("SELECT COUNT(*) FROM characters")).fetchone()[0] == 0
    assert conn.execute(
        text("SELECT COUNT(*) FROM char_skills_cache")).fetchone()[0] == 0


def test_the_schema_shim_is_a_no_op_on_postgres(conn):
    """`ensure_characters_table` forwards to `PRAGMA database_list`, which is a
    syntax error off SQLite. The dialect guard is what makes it safe to keep
    calling.

    It used to check `skills_api.ensure_skills_table` too. That one had no
    caller left anywhere in `app/` — migrations replaced these per-table shims
    back when the schema got one home — and was removed in v0.9.76 along with
    five siblings in the same state.
    """
    ts.ensure_characters_table(conn)

    assert conn.execute(text("SELECT COUNT(*) FROM characters")).fetchone()[0] == 0


# ── token_store: the two updaters ────────────────────────────────────────────

def test_the_corporation_id_is_stored_and_survives_a_new_connection(engine):
    """A writer with a commit. Dropping that commit passes every same-connection
    assertion and loses the write when the request ends."""
    with engine.connect() as c:
        _add_character(c)
        ts.update_corporation_id(c, CHAR, CORP)

    with engine.connect() as c:
        row = c.execute(
            text("SELECT corporation_id FROM characters"
                 " WHERE character_id=:cid"), {"cid": CHAR}).fetchone()
    assert row[0] == CORP, f"on {engine.dialect.name}: the write did not commit"


def test_storing_a_corporation_does_not_disturb_the_refresh_token(conn):
    """The column that must never be collateral damage. An `UPDATE` naming one
    column is safe; the failure mode being guarded against is somebody
    "simplifying" it into a replace, which is what cost this project its
    characters once already."""
    _add_character(conn, refresh="the-real-refresh-token")

    ts.update_corporation_id(conn, CHAR, CORP)

    row = conn.execute(
        text("SELECT refresh_token, character_name FROM characters"
             " WHERE character_id=:cid"), {"cid": CHAR}).fetchone()
    assert row[0] == "the-real-refresh-token"
    assert row[1] == "Astroasia"


def test_the_last_sync_time_is_stored_and_moves_forward(engine):
    before = time.time()
    with engine.connect() as c:
        _add_character(c)
        ts.update_last_sync(c, CHAR)

    with engine.connect() as c:
        row = c.execute(
            text("SELECT last_sync_at FROM characters"
                 " WHERE character_id=:cid"), {"cid": CHAR}).fetchone()
    assert row[0] is not None
    assert row[0] >= before


def test_updating_an_unknown_character_is_a_no_op(conn):
    """`UPDATE ... WHERE character_id=?` matching nothing must not raise — the
    sync worker calls this for characters that may have been deleted mid-run."""
    ts.update_corporation_id(conn, 999_999, CORP)
    ts.update_last_sync(conn, 999_999)

    assert conn.execute(
        text("SELECT COUNT(*) FROM characters")).fetchone()[0] == 0


# ── token_store: the legacy JSON migration ───────────────────────────────────

def _write_config(payload: dict) -> None:
    with open(ts.config_path(), "w") as f:
        json.dump(payload, f)


def test_a_legacy_config_is_migrated_into_the_characters_table(conn):
    """The one-time upgrade path from the single-character desktop build."""
    _write_config({"client_id": "abc", "character_id": CHAR,
                   "character_name": "Tracy Juan", "refresh_token": "legacy-r"})

    ts._migrate_legacy_json(conn)

    row = conn.execute(
        text("SELECT character_name, refresh_token FROM characters"
             " WHERE character_id=:cid"), {"cid": CHAR}).fetchone()
    assert tuple(row) == ("Tracy Juan", "legacy-r")


def test_migrating_strips_the_token_from_the_json_but_keeps_the_client_id(conn):
    """The whole point of the migration: the refresh token stops living in a
    plain file once the database has it. `client_id` is not a secret and is
    still needed, so it stays."""
    _write_config({"client_id": "abc", "character_id": CHAR,
                   "character_name": "Tracy Juan", "refresh_token": "legacy-r",
                   "access_token": "a", "token_expires_at": 1})

    ts._migrate_legacy_json(conn)

    with open(ts.config_path()) as f:
        left = json.load(f)
    assert left == {"client_id": "abc"}, f"token fields survived: {left}"


def test_a_character_already_in_the_database_is_not_re_migrated(conn):
    """Re-running must not overwrite the stored token with a stale file one —
    but it must still strip the file, or the token lives on in two places."""
    _add_character(conn, refresh="current-token")
    _write_config({"client_id": "abc", "character_id": CHAR,
                   "character_name": "Stale", "refresh_token": "stale-token"})

    ts._migrate_legacy_json(conn)

    row = conn.execute(
        text("SELECT character_name, refresh_token FROM characters"
             " WHERE character_id=:cid"), {"cid": CHAR}).fetchone()
    assert tuple(row) == ("Astroasia", "current-token"), "the stale file won"
    with open(ts.config_path()) as f:
        assert json.load(f) == {"client_id": "abc"}


def test_nothing_to_migrate_leaves_everything_alone(conn):
    _write_config({"client_id": "abc"})

    ts._migrate_legacy_json(conn)

    assert conn.execute(
        text("SELECT COUNT(*) FROM characters")).fetchone()[0] == 0
    with open(ts.config_path()) as f:
        assert json.load(f) == {"client_id": "abc"}


# ── jobs ─────────────────────────────────────────────────────────────────────

def test_activity_labels_cover_the_ids_the_pages_group_on(conn):
    assert jobs_api.activity_label(1) == "Manufacturing"
    assert jobs_api.activity_label(8) == "Invention"
    # 9 and 11 are both reactions, and the /jobs page groups them together.
    assert jobs_api.activity_label(9) == jobs_api.activity_label(11) == "Reactions"


def test_an_unknown_activity_is_named_rather_than_dropped(conn):
    """CCP adds activity ids. Showing "Activity 42" is a worse page than
    crashing is a worse outage."""
    assert jobs_api.activity_label(42) == "Activity 42"


def test_saved_jobs_read_back(conn):
    jobs = [{"job_id": 1, "activity_id": 1}, {"job_id": 2, "activity_id": 9}]

    jobs_api.save_cached_jobs(conn, CHAR, jobs)
    got, cached_at = jobs_api.load_cached_jobs(conn, CHAR)

    assert got == jobs
    assert cached_at > 0


def test_an_unsynced_character_reads_as_none_not_empty(conn):
    """`None` means "the worker has not looked"; `[]` means "looked, no jobs".
    A page that renders the first as the second tells the user they are idle."""
    got, cached_at = jobs_api.load_cached_jobs(conn, CHAR)

    assert got is None
    assert cached_at == 0.0


def test_a_character_with_no_jobs_reads_as_empty_not_none(conn):
    jobs_api.save_cached_jobs(conn, CHAR, [])

    got, _ = jobs_api.load_cached_jobs(conn, CHAR)

    assert got == [], "an empty job list came back as 'never synced'"


def test_a_corrupt_cache_reads_as_unsynced(conn):
    """Better to say "not synced" than to raise on a page render."""
    conn.execute(
        text("INSERT INTO char_jobs_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :at)"),
        {"cid": CHAR, "data": "{not json", "at": time.time()})
    conn.commit()

    assert jobs_api.load_cached_jobs(conn, CHAR) == (None, 0.0)


def test_the_commit_for_a_job_save_lives_in_the_fetcher(engine):
    """`save_cached_jobs` deliberately does not commit; `fetch_industry_jobs`
    does. Pinned because both halves of that split are easy to get wrong in a
    conversion — adding a commit inside the writer moves the transaction
    boundary, and dropping the caller's loses the write with no symptom.
    """
    client = _Client(_Resp(200, [{"job_id": 7, "activity_id": 1}]))

    with engine.connect() as c:
        got = asyncio.run(jobs_api.fetch_industry_jobs(
            client, CHAR, "tok", conn=c))

    assert got == [{"job_id": 7, "activity_id": 1}]
    with engine.connect() as c:
        cached, _ = jobs_api.load_cached_jobs(c, CHAR)
    assert cached == [{"job_id": 7, "activity_id": 1}], (
        f"on {engine.dialect.name}: the fetcher did not commit")


def test_a_failed_job_fetch_returns_none_rather_than_empty(conn):
    """Same distinction as the cache: a transient ESI error must not be stored
    or reported as "no jobs"."""
    client = _Client(_Resp(500))

    got = asyncio.run(jobs_api.fetch_industry_jobs(
        client, CHAR, "tok", conn=conn))

    assert got is None
    assert jobs_api.load_cached_jobs(conn, CHAR) == (None, 0.0)


# ── skills: the cache blob ───────────────────────────────────────────────────

def test_a_current_blob_round_trips(conn):
    skills_api._save_cache(conn, CHAR, {3380: 5, 3388: 4})

    assert skills_api.get_cached_skills(conn, CHAR) == {3380: 5, 3388: 4}


def test_saved_skills_survive_a_new_connection(engine):
    """The lost-`commit()` net for this writer."""
    with engine.connect() as c:
        skills_api._save_cache(c, CHAR, {3380: 5})

    with engine.connect() as c:
        assert skills_api.get_cached_skills(c, CHAR) == {3380: 5}, (
            f"on {engine.dialect.name}: the write did not commit")


def test_a_legacy_flat_blob_is_still_readable():
    """Version 0 was a bare `{skill_id: level}` map with no `__v`. Rows written
    by an older build are still in real databases."""
    version, skills = skills_api._parse_blob('{"3380": 5}')

    assert version == 0
    assert skills == {3380: 5}


def test_a_versioned_blob_reports_its_version():
    version, skills = skills_api._parse_blob(
        '{"__v": 2, "skills": {"3380": 5}}')

    assert version == 2
    assert skills == {3380: 5}


def test_an_unparseable_blob_is_empty_rather_than_fatal():
    assert skills_api._parse_blob("{not json") == (0, {})
    assert skills_api._parse_blob("[1,2,3]") == (0, {})


def test_a_stale_cache_is_not_fresh(conn):
    conn.execute(
        text("INSERT INTO char_skills_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :at)"),
        {"cid": CHAR, "data": '{"__v": 2, "skills": {"3380": 5}}',
         "at": time.time() - skills_api.CACHE_TTL - 1})
    conn.commit()

    assert skills_api._load_cache_fresh(conn, CHAR) is None
    # ...but the stale rows are still readable, which is what the ESI-failure
    # fallback depends on.
    assert skills_api.get_cached_skills(conn, CHAR) == {3380: 5}


def test_an_old_schema_version_forces_a_refresh(conn):
    """Version 0 held a *filtered subset* of skills, so serving it as if it were
    complete under-reports what the character can build."""
    conn.execute(
        text("INSERT INTO char_skills_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :at)"),
        {"cid": CHAR, "data": '{"3380": 5}', "at": time.time()})
    conn.commit()

    assert skills_api._load_cache_fresh(conn, CHAR) is None


def test_an_unknown_character_has_no_cached_skills(conn):
    assert skills_api.get_cached_skills(conn, CHAR) == {}


# ── skills: the fetcher ──────────────────────────────────────────────────────

def test_a_fresh_cache_is_not_refetched(conn):
    client = _Client()
    skills_api._save_cache(conn, CHAR, {3380: 5})

    got = asyncio.run(skills_api.fetch_skills(client, CHAR, "tok", conn))

    assert got == {3380: 5}
    assert client.calls == 0, "it went to ESI despite a fresh cache"


def test_force_refresh_ignores_a_fresh_cache(conn):
    client = _Client(_Resp(200, {"skills": [
        {"skill_id": 3380, "trained_skill_level": 5}]}))
    skills_api._save_cache(conn, CHAR, {3380: 1})

    got = asyncio.run(skills_api.fetch_skills(
        client, CHAR, "tok", conn, force_refresh=True))

    assert got == {3380: 5}
    assert client.calls == 1


def test_a_fetch_stores_every_skill_not_just_the_ones_we_use(conn):
    """Version 2 exists because version 0 stored a filtered subset."""
    client = _Client(_Resp(200, {"skills": [
        {"skill_id": 3380, "trained_skill_level": 5},
        {"skill_id": 999999, "trained_skill_level": 3},
    ]}))

    got = asyncio.run(skills_api.fetch_skills(client, CHAR, "tok", conn))

    assert got == {3380: 5, 999999: 3}
    assert skills_api.get_cached_skills(conn, CHAR) == {3380: 5, 999999: 3}


def test_a_failed_skill_fetch_falls_back_to_a_stale_cache(conn):
    """Planning with slightly old skills beats planning with none."""
    conn.execute(
        text("INSERT INTO char_skills_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :at)"),
        {"cid": CHAR, "data": '{"__v": 2, "skills": {"3380": 4}}',
         "at": time.time() - skills_api.CACHE_TTL - 1})
    conn.commit()
    client = _Client(_Resp(503))

    got = asyncio.run(skills_api.fetch_skills(client, CHAR, "tok", conn))

    assert got == {3380: 4}


def test_a_raising_skill_fetch_falls_back_too(conn):
    skills_api._save_cache(conn, CHAR, {3380: 4})
    client = _Client(RuntimeError("connection reset"))

    got = asyncio.run(skills_api.fetch_skills(
        client, CHAR, "tok", conn, force_refresh=True))

    assert got == {3380: 4}


# ── skills: the manufacturing skill set ──────────────────────────────────────

def test_the_manufacturing_skills_include_the_general_two(conn):
    """Industry and Advanced Industry are not in the SDE's science-bonus table,
    so they are added by hand. Losing them silently drops the two skills that
    affect every single job."""
    ids = skills_api.get_mfg_skill_ids(conn)

    assert {3380, 3388} <= ids


def test_a_missing_sde_table_yields_the_general_skills_rather_than_raising(conn):
    """The SDE tables are absent until `import_sde.py` has run, and this is
    called while rendering. It has to degrade, not raise.

    This one is a portability tripwire as much as a behaviour test: the
    `except sqlite3.OperationalError` that makes it work is driver-specific, and
    on Postgres the same absence raises something else entirely.
    """
    conn.execute(text("DROP TABLE IF EXISTS sde_skill_time_bonus"))
    conn.commit()

    assert skills_api.get_mfg_skill_ids(conn) == {3380, 3388}

    # ...and the connection is still usable afterwards. This half is the point
    # on Postgres: a failed statement aborts the whole transaction, so a
    # `try/except: return the empty answer` that skips the rollback leaves the
    # damage to surface in whatever unrelated query runs next. Asserting only
    # the return value above would pass with the rollback removed.
    assert conn.execute(
        text("SELECT COUNT(*) FROM characters")).fetchone()[0] == 0, (
        f"on {_backend(conn)}: the connection was left unusable")


# ── delete_character across a missing cache table ────────────────────────────

def test_deleting_a_character_survives_a_missing_cache_table(engine):
    """The `recover_from_missing_table` path, which the conversion introduced.

    `delete_character` removes the character row and then cascades into three
    per-character cache tables, any of which can be absent on an older database.
    Swallowing that used to be a bare `except sqlite3.OperationalError`, which
    is fine on SQLite and quietly catastrophic on Postgres: a failed statement
    aborts the whole transaction, so the two remaining DELETEs *and the commit*
    would fail, leaving the character — and its refresh token — in place.

    The rollback that fixes it also discards the character DELETE, so that
    statement is re-issued afterwards. This is the test that says so, because
    nothing else exercises a database missing one of those tables.
    """
    from app.db.schema import metadata

    with engine.connect() as c:
        _add_character(c)
        c.execute(text("DROP TABLE char_skills_cache"))
        c.commit()

    try:
        with engine.connect() as c:
            ts.delete_character(c, CHAR)

        with engine.connect() as c:
            left = c.execute(
                text("SELECT COUNT(*) FROM characters WHERE character_id=:cid"),
                {"cid": CHAR}).fetchone()[0]
        assert left == 0, (
            f"on {engine.dialect.name}: the character survived, so its refresh "
            f"token did too")
    finally:
        # Module-scoped engine: put the table back or every later test in this
        # file trips over the hole this one dug.
        metadata.tables["char_skills_cache"].create(engine, checkfirst=True)
