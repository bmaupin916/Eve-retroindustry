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

Written against the `sqlite3` versions deliberately, so the conversion has
assertions to preserve rather than assertions invented afterwards to fit it.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from app.auth import token_store as ts
from app.character import jobs as jobs_api
from app.character import skills as skills_api

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

@pytest.fixture
def conn(tmp_path):
    """A file, not `:memory:` — the commit assertions open a second connection."""
    c = sqlite3.connect(str(tmp_path / "eve_cache.db"))
    ts.ensure_characters_table(c)
    skills_api.ensure_skills_table(c)
    yield c
    c.close()


def _reopen(conn) -> sqlite3.Connection:
    path = [r[2] for r in conn.execute("PRAGMA database_list")
            if r[1] == "main"][0]
    return sqlite3.connect(path)


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
        "INSERT INTO characters (character_id, character_name, refresh_token,"
        " added_at) VALUES (?,?,?,?)", (char_id, name, refresh, 1000.0))
    conn.commit()


# ── the schema shims ─────────────────────────────────────────────────────────

def test_the_shims_create_the_tables_they_name(conn):
    """Both are one-line forwards to `app/db/schema.py`, and both are about to
    grow a dialect guard. This pins what they are for."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    assert "characters" in tables
    assert "char_skills_cache" in tables


# ── token_store: the two updaters ────────────────────────────────────────────

def test_the_corporation_id_is_stored_and_survives_a_new_connection(conn):
    """A writer with a commit. Dropping that commit passes every same-connection
    assertion and loses the write when the request ends."""
    _add_character(conn)

    ts.update_corporation_id(conn, CHAR, CORP)

    other = _reopen(conn)
    try:
        row = other.execute("SELECT corporation_id FROM characters"
                            " WHERE character_id=?", (CHAR,)).fetchone()
        assert row[0] == CORP
    finally:
        other.close()


def test_storing_a_corporation_does_not_disturb_the_refresh_token(conn):
    """The column that must never be collateral damage. An `UPDATE` naming one
    column is safe; the failure mode being guarded against is somebody
    "simplifying" it into a replace, which is what cost this project its
    characters once already."""
    _add_character(conn, refresh="the-real-refresh-token")

    ts.update_corporation_id(conn, CHAR, CORP)

    row = conn.execute("SELECT refresh_token, character_name FROM characters"
                       " WHERE character_id=?", (CHAR,)).fetchone()
    assert row[0] == "the-real-refresh-token"
    assert row[1] == "Astroasia"


def test_the_last_sync_time_is_stored_and_moves_forward(conn):
    _add_character(conn)
    before = time.time()

    ts.update_last_sync(conn, CHAR)

    other = _reopen(conn)
    try:
        row = other.execute("SELECT last_sync_at FROM characters"
                            " WHERE character_id=?", (CHAR,)).fetchone()
    finally:
        other.close()
    assert row[0] is not None
    assert row[0] >= before


def test_updating_an_unknown_character_is_a_no_op(conn):
    """`UPDATE ... WHERE character_id=?` matching nothing must not raise — the
    sync worker calls this for characters that may have been deleted mid-run."""
    ts.update_corporation_id(conn, 999_999, CORP)
    ts.update_last_sync(conn, 999_999)

    assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 0


# ── token_store: the legacy JSON migration ───────────────────────────────────

def _write_config(payload: dict) -> None:
    with open(ts.config_path(), "w") as f:
        json.dump(payload, f)


def test_a_legacy_config_is_migrated_into_the_characters_table(conn):
    """The one-time upgrade path from the single-character desktop build."""
    _write_config({"client_id": "abc", "character_id": CHAR,
                   "character_name": "Tracy Juan", "refresh_token": "legacy-r"})

    ts._migrate_legacy_json(conn)

    row = conn.execute("SELECT character_name, refresh_token FROM characters"
                       " WHERE character_id=?", (CHAR,)).fetchone()
    assert row == ("Tracy Juan", "legacy-r")


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

    row = conn.execute("SELECT character_name, refresh_token FROM characters"
                       " WHERE character_id=?", (CHAR,)).fetchone()
    assert row == ("Astroasia", "current-token"), "the stale file won"
    with open(ts.config_path()) as f:
        assert json.load(f) == {"client_id": "abc"}


def test_nothing_to_migrate_leaves_everything_alone(conn):
    _write_config({"client_id": "abc"})

    ts._migrate_legacy_json(conn)

    assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 0
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
    conn.execute("INSERT INTO char_jobs_cache (character_id, data_json,"
                 " cached_at) VALUES (?,?,?)", (CHAR, "{not json", time.time()))
    conn.commit()

    assert jobs_api.load_cached_jobs(conn, CHAR) == (None, 0.0)


def test_the_commit_for_a_job_save_lives_in_the_fetcher(conn):
    """`save_cached_jobs` deliberately does not commit; `fetch_industry_jobs`
    does. Pinned because both halves of that split are easy to get wrong in a
    conversion — adding a commit inside the writer moves the transaction
    boundary, and dropping the caller's loses the write with no symptom.
    """
    client = _Client(_Resp(200, [{"job_id": 7, "activity_id": 1}]))

    got = asyncio.run(jobs_api.fetch_industry_jobs(
        client, CHAR, "tok", conn=conn))

    assert got == [{"job_id": 7, "activity_id": 1}]
    other = _reopen(conn)
    try:
        cached, _ = jobs_api.load_cached_jobs(other, CHAR)
        assert cached == [{"job_id": 7, "activity_id": 1}], (
            "the fetcher did not commit")
    finally:
        other.close()


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


def test_saved_skills_survive_a_new_connection(conn):
    """The lost-`commit()` net for this writer."""
    skills_api._save_cache(conn, CHAR, {3380: 5})

    other = _reopen(conn)
    try:
        assert skills_api.get_cached_skills(other, CHAR) == {3380: 5}
    finally:
        other.close()


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
    conn.execute("INSERT INTO char_skills_cache (character_id, data_json,"
                 " cached_at) VALUES (?,?,?)",
                 (CHAR, '{"__v": 2, "skills": {"3380": 5}}',
                  time.time() - skills_api.CACHE_TTL - 1))
    conn.commit()

    assert skills_api._load_cache_fresh(conn, CHAR) is None
    # ...but the stale rows are still readable, which is what the ESI-failure
    # fallback depends on.
    assert skills_api.get_cached_skills(conn, CHAR) == {3380: 5}


def test_an_old_schema_version_forces_a_refresh(conn):
    """Version 0 held a *filtered subset* of skills, so serving it as if it were
    complete under-reports what the character can build."""
    conn.execute("INSERT INTO char_skills_cache (character_id, data_json,"
                 " cached_at) VALUES (?,?,?)",
                 (CHAR, '{"3380": 5}', time.time()))
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
    conn.execute("INSERT INTO char_skills_cache (character_id, data_json,"
                 " cached_at) VALUES (?,?,?)",
                 (CHAR, '{"__v": 2, "skills": {"3380": 4}}',
                  time.time() - skills_api.CACHE_TTL - 1))
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
    conn.execute("DROP TABLE IF EXISTS sde_skill_time_bonus")
    conn.commit()

    assert skills_api.get_mfg_skill_ids(conn) == {3380, 3388}
