"""`app/character/assets.py`, before it moves onto the portable query layer.

The coverage probe says nine of its functions are never executed by the suite:
`fetch_assets`, `fetch_corp_assets`, `_save_cache`, `_save_corp_cache`,
`_load_corp_cache`, `assets_at_location`, `assets_at_locations`,
`ensure_assets_table` and `ensure_corp_assets_table`. Both cache writers and
both fetchers, in other words — and `tests/test_sync_worker.py` monkeypatches
the fetchers onto the worker module, which is why the worker is well covered
while these never run.

**These assertions are unchanged by the conversion.** They were written
against the `sqlite3` version first, exactly so they could be preserved rather
than invented afterwards to fit whatever the rewrite did. Only the fixture
underneath moved, and it now runs each of them on both backends — bar one,
marked `sqlite_only`, which lowers a SQLite compile-time limit and has no
Postgres equivalent.

Three things here are conversion traps rather than ordinary behaviour:

* **`_save_cache` is DELETE-then-INSERT, not an upsert**, and it commits. Two
  statements where one row is meant to survive.
* **`load_cached_container_names` chunks its `IN (...)` at 900** because SQLite
  caps a statement at 999 parameters. An expanding bindparam changes what that
  limit means, and a big account really does hold more than 900 containers, so
  the chunking has a test that would notice it being dropped.
* **`save_cached_container_names` does not commit**, and neither does
  `fetch_container_names`, its only caller. The sync worker's per-character
  block commits for them. That split is pinned below, because a conversion that
  adds a commit inside the writer moves the transaction boundary and one that
  drops the worker's loses the names silently.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest
from sqlalchemy import create_engine, text

from app.character import assets as assets_api
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_character_assets"

CHAR = 2_112_625_428
CORP = 98_000_001
JITA = 60003760
STRUCTURE = 1_049_982_731_184
TRITANIUM = 34


# ── stubs ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, payload=None, pages: int = 1):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = {"x-pages": str(pages)}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Enough of httpx.AsyncClient for these fetchers, routed by URL shape."""

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


def _asset(item_id, type_id=TRITANIUM, location_id=JITA, qty=100,
           singleton=False, flag="Hangar"):
    return {"item_id": item_id, "type_id": type_id, "location_id": location_id,
            "location_flag": flag, "quantity": qty, "is_singleton": singleton,
            "is_blueprint_copy": False}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    """An engine per backend, built once for the module.

    Function scope here would drop and rebuild a Postgres schema — all ten
    migrations — for every test in the file; clearing the tables between tests
    is the same isolation for a fraction of the cost.
    """
    from app.db.migrate import upgrade_to_head

    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'eve_cache.db'}"
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
_CLEARED = ("char_assets_cache", "corp_assets_cache", "container_name_cache")


@pytest.fixture(autouse=True)
def _empty_tables(engine):
    """Before, not after: a test that dies half-way must not leave its rows for
    the next one to read."""
    with engine.connect() as c:
        for table in _CLEARED:
            c.execute(text(f"DELETE FROM {table}"))
        c.commit()
    yield


@pytest.fixture
def conn(engine, request):
    if engine.dialect.name != "sqlite" and \
            request.node.get_closest_marker("sqlite_only"):
        pytest.skip("lowers a SQLite compile-time limit; no Postgres equivalent")
    with engine.connect() as c:
        yield c


def _backend(conn) -> str:
    return conn.engine.dialect.name


# ── the schema shims ─────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing file: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    for table in _CLEARED:
        assert conn.execute(
            text(f"SELECT COUNT(*) FROM {table}")).fetchone()[0] == 0


def test_the_schema_shims_are_safe_on_either_backend(conn):
    """Both forward to `PRAGMA database_list`, which is a syntax error on
    Postgres. The dialect guard is what makes them safe to keep calling."""
    assets_api.ensure_assets_table(conn)
    assets_api.ensure_corp_assets_table(conn)

    assert conn.execute(
        text("SELECT COUNT(*) FROM char_assets_cache")).fetchone()[0] == 0


# ── the character cache ──────────────────────────────────────────────────────

def test_saved_assets_read_back(conn):
    assets_api._save_cache(conn, CHAR, [_asset(1), _asset(2)])

    got, cached_at = assets_api.load_cached_assets(conn, CHAR)

    assert [a.item_id for a in got] == [1, 2]
    assert cached_at > 0


def test_saving_twice_replaces_rather_than_duplicating(conn):
    """DELETE-then-INSERT, not an upsert. If the DELETE were dropped the second
    save would leave two rows for one character, and which one wins is then a
    question about row order."""
    assets_api._save_cache(conn, CHAR, [_asset(1)])
    assets_api._save_cache(conn, CHAR, [_asset(2)])

    rows = conn.execute(
        text("SELECT COUNT(*) FROM char_assets_cache WHERE character_id=:cid"),
        {"cid": CHAR}).fetchone()[0]
    assert rows == 1, "the character has two cache rows"
    got, _ = assets_api.load_cached_assets(conn, CHAR)
    assert [a.item_id for a in got] == [2]


def test_saved_assets_survive_a_new_connection(engine):
    """The lost-`commit()` net for this writer."""
    with engine.connect() as c:
        assets_api._save_cache(c, CHAR, [_asset(1)])

    with engine.connect() as c:
        got, _ = assets_api.load_cached_assets(c, CHAR)
    assert [a.item_id for a in got] == [1], (
        f"on {engine.dialect.name}: the write did not commit")


def test_an_unsynced_character_reads_as_none_not_empty(conn):
    """`None` means "the worker has not looked"; `[]` means "looked, nothing
    there". A hangar shown as empty when nobody looked is a statement about
    your assets rather than about the sync."""
    got, cached_at = assets_api.load_cached_assets(conn, CHAR)

    assert got is None
    assert cached_at == 0.0


def test_a_character_with_nothing_reads_as_empty_not_none(conn):
    assets_api._save_cache(conn, CHAR, [])

    got, _ = assets_api.load_cached_assets(conn, CHAR)

    assert got == []


def test_a_corrupt_cache_reads_as_unsynced(conn):
    conn.execute(
        text("INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :at)"),
        {"cid": CHAR, "data": "{not json", "at": time.time()})
    conn.commit()

    assert assets_api.load_cached_assets(conn, CHAR) == (None, 0.0)


def test_the_read_path_ignores_the_ttl(conn):
    """`load_cached_assets` deliberately returns an aged cache. Applying the
    fetcher's TTL here made a stale cache indistinguishable from an empty one,
    so the page fetched — which is what the worker exists to prevent."""
    conn.execute(
        text("INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :at)"),
        {"cid": CHAR, "data": json.dumps([_asset(1)]),
         "at": time.time() - assets_api.CACHE_TTL * 10})
    conn.commit()

    got, cached_at = assets_api.load_cached_assets(conn, CHAR)

    assert [a.item_id for a in got] == [1]
    assert cached_at > 0, "the age must still be reported"


def test_the_fetcher_path_enforces_the_ttl(conn):
    """...and `_load_cache`, which the fetcher uses, does not."""
    conn.execute(
        text("INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :at)"),
        {"cid": CHAR, "data": json.dumps([_asset(1)]),
         "at": time.time() - assets_api.CACHE_TTL - 1})
    conn.commit()

    assert assets_api._load_cache(conn, CHAR) is None


# ── fetch_assets ─────────────────────────────────────────────────────────────

def test_a_fresh_cache_is_not_refetched(conn):
    client = _Client()
    assets_api._save_cache(conn, CHAR, [_asset(1)])

    got = asyncio.run(assets_api.fetch_assets(client, CHAR, "tok", conn))

    assert [a.item_id for a in got] == [1]
    assert client.calls == 0, "it went to ESI despite a fresh cache"


def test_force_refresh_ignores_a_fresh_cache(conn):
    """The worker passes this. Without it the fetch consults the same TTL it is
    about to write, which is circular."""
    client = _Client(_Resp(200, [_asset(2)]))
    assets_api._save_cache(conn, CHAR, [_asset(1)])

    got = asyncio.run(assets_api.fetch_assets(
        client, CHAR, "tok", conn, force_refresh=True))

    assert [a.item_id for a in got] == [2]
    assert client.calls == 1


def test_every_page_is_fetched_and_stored(conn):
    """ESI pages assets and reports the count in `x-pages`. Stopping after the
    first page silently loses most of a real account's hangar."""
    client = _Client(
        _Resp(200, [_asset(1), _asset(2)], pages=3),
        _Resp(200, [_asset(3)], pages=3),
        _Resp(200, [_asset(4)], pages=3),
    )

    got = asyncio.run(assets_api.fetch_assets(client, CHAR, "tok", conn))

    assert [a.item_id for a in got] == [1, 2, 3, 4]
    assert client.calls == 3
    cached, _ = assets_api.load_cached_assets(conn, CHAR)
    assert [a.item_id for a in cached] == [1, 2, 3, 4], "only part was cached"


def test_a_single_page_costs_one_call(conn):
    client = _Client(_Resp(200, [_asset(1)], pages=1))

    asyncio.run(assets_api.fetch_assets(client, CHAR, "tok", conn))

    assert client.calls == 1


# ── the corporation cache ────────────────────────────────────────────────────

def test_saved_corp_assets_read_back_and_survive_a_new_connection(engine):
    with engine.connect() as c:
        assets_api._save_corp_cache(c, CORP, [_asset(9)])

    with engine.connect() as c:
        got, cached_at = assets_api.load_cached_corp_assets(c, CORP)
    assert [a.item_id for a in got] == [9], (
        f"on {engine.dialect.name}: the write did not commit")
    assert cached_at > 0


def test_saving_corp_assets_twice_replaces(conn):
    assets_api._save_corp_cache(conn, CORP, [_asset(1)])
    assets_api._save_corp_cache(conn, CORP, [_asset(2)])

    rows = conn.execute(
        text("SELECT COUNT(*) FROM corp_assets_cache"
             " WHERE corporation_id=:corp"), {"corp": CORP}).fetchone()[0]
    assert rows == 1


def test_a_stale_corp_cache_is_not_fresh(conn):
    conn.execute(
        text("INSERT INTO corp_assets_cache (corporation_id, data_json,"
             " cached_at) VALUES (:corp, :data, :at)"),
        {"corp": CORP, "data": json.dumps([_asset(1)]),
         "at": time.time() - assets_api.CACHE_TTL - 1})
    conn.commit()

    assert assets_api._load_corp_cache(conn, CORP) is None


# ── fetch_corp_assets ────────────────────────────────────────────────────────

def test_corp_assets_resolve_the_corporation_first(conn):
    """The corp id is not passed in — it comes from the character sheet, which
    is why this fetcher returns it alongside the assets."""
    client = _Client(
        _Resp(200, {"corporation_id": CORP}),
        _Resp(200, [_asset(7)], pages=1),
    )

    corp_id, got = asyncio.run(
        assets_api.fetch_corp_assets(client, CHAR, "tok", conn))

    assert corp_id == CORP
    assert [a.item_id for a in got] == [7]


def test_no_corp_role_yields_the_corp_id_and_nothing_else(conn):
    """Corp assets need a role most characters do not have. A 403 is not a
    failure of the sync, and it must not be cached as "this corp has no
    assets" — the next character with the role has to be able to fill it."""
    client = _Client(
        _Resp(200, {"corporation_id": CORP}),
        _Resp(403),
    )

    corp_id, got = asyncio.run(
        assets_api.fetch_corp_assets(client, CHAR, "tok", conn))

    assert corp_id == CORP
    assert got == []
    assert conn.execute(
        text("SELECT COUNT(*) FROM corp_assets_cache")).fetchone()[0] == 0, (
        "a refused fetch was written to the cache")


def test_corp_assets_are_paged_too(conn):
    client = _Client(
        _Resp(200, {"corporation_id": CORP}),
        _Resp(200, [_asset(1)], pages=2),
        _Resp(200, [_asset(2)], pages=2),
    )

    _corp, got = asyncio.run(
        assets_api.fetch_corp_assets(client, CHAR, "tok", conn))

    assert [a.item_id for a in got] == [1, 2]


# ── location roll-ups ────────────────────────────────────────────────────────

def test_quantities_are_summed_per_type_at_one_location(conn):
    parsed = assets_api._parse_assets([
        _asset(1, TRITANIUM, JITA, 100),
        _asset(2, TRITANIUM, JITA, 250),
        _asset(3, TRITANIUM, STRUCTURE, 999),
    ])

    assert assets_api.assets_at_location(parsed, JITA) == {TRITANIUM: 350}


def test_singletons_are_not_stock(conn):
    """A singleton is an assembled item — a ship, a fitted module. Counting a
    fitted afterburner as raw stock would let a plan consume the ship you are
    flying."""
    parsed = assets_api._parse_assets([
        _asset(1, TRITANIUM, JITA, 100),
        _asset(2, TRITANIUM, JITA, 5, singleton=True),
    ])

    assert assets_api.assets_at_location(parsed, JITA) == {TRITANIUM: 100}


def test_a_location_with_nothing_is_empty(conn):
    parsed = assets_api._parse_assets([_asset(1, TRITANIUM, JITA)])

    assert assets_api.assets_at_location(parsed, STRUCTURE) == {}


def test_multiple_locations_aggregate(conn):
    """The plan's stock-source picker: tick two stations, get one pool."""
    parsed = assets_api._parse_assets([
        _asset(1, TRITANIUM, JITA, 100),
        _asset(2, TRITANIUM, STRUCTURE, 200),
        _asset(3, TRITANIUM, 60003761, 400),
    ])

    got = assets_api.assets_at_locations(parsed, {JITA, STRUCTURE})

    assert got == {TRITANIUM: 300}, "an unticked station leaked into the pool"


def test_multiple_locations_ignore_singletons_too(conn):
    parsed = assets_api._parse_assets([
        _asset(1, TRITANIUM, JITA, 100),
        _asset(2, TRITANIUM, STRUCTURE, 7, singleton=True),
    ])

    assert assets_api.assets_at_locations(parsed, {JITA, STRUCTURE}) == {TRITANIUM: 100}


# ── container names ──────────────────────────────────────────────────────────

def test_container_names_read_back(conn):
    assets_api.save_cached_container_names(conn, {1: "Ore Can", 2: "Spares"})
    conn.commit()

    assert assets_api.load_cached_container_names(conn, [1, 2]) == {
        1: "Ore Can", 2: "Spares"}


def test_asking_for_no_containers_touches_nothing(conn):
    assert assets_api.load_cached_container_names(conn, []) == {}


@pytest.mark.sqlite_only
def test_the_container_lookup_survives_a_low_parameter_limit(conn):
    """The `IN (...)` is chunked at 900 because a statement has a cap on how
    many parameters it may bind.

    **Measured, because the comment in the module is out of date:** the cap is
    a compile-time setting, it was 999 before SQLite 3.32 (2020), and the build
    here reports 32,766. So a test that merely asks for 1,500 ids proves
    nothing about the chunking — it passes with the chunking removed, which is
    exactly what the first version of this test did.

    Lowering the limit for this connection is the honest version: it reproduces
    the build the chunking exists for, at a realistic number of containers,
    rather than needing 33,000 of them. Without chunking this raises
    "too many SQL variables".
    """
    names = {i: f"Can {i}" for i in range(1, 1501)}
    assets_api.save_cached_container_names(conn, names)
    conn.commit()

    driver = conn.connection.driver_connection
    driver.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    try:
        got = assets_api.load_cached_container_names(conn, list(names))
    finally:
        driver.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32766)

    assert len(got) == 1500
    assert got[1] == "Can 1" and got[1500] == "Can 1500"


def test_saving_no_container_names_is_not_an_error(conn):
    """An empty batch is the common case — nothing on this character is named.

    It was a no-op for the per-row loop this replaced, and it is *not* one for
    a SQLAlchemy executemany: with no rows to infer the parameter shape from it
    raises. Guarded in the writer rather than at the call site, and this is what
    says so.
    """
    assets_api.save_cached_container_names(conn, {})
    conn.commit()

    assert assets_api.load_cached_container_names(conn, [1]) == {}


def test_naming_a_container_again_replaces_the_name(conn):
    assets_api.save_cached_container_names(conn, {1: "Old"})
    assets_api.save_cached_container_names(conn, {1: "New"})
    conn.commit()

    assert assets_api.load_cached_container_names(conn, [1]) == {1: "New"}


def test_the_commit_for_container_names_lives_upstream(engine):
    """`save_cached_container_names` does not commit, and neither does
    `fetch_container_names`. The sync worker's per-character block does.

    Pinned because both halves are easy to get wrong in a conversion: adding a
    commit inside the writer moves the transaction boundary, and dropping the
    worker's loses every custom name with no symptom — the page just shows bare
    hull types again, which is how this was found the first time.
    """
    with engine.connect() as writer:
        assets_api.save_cached_container_names(writer, {1: "Ore Can"})

        with engine.connect() as other:
            assert assets_api.load_cached_container_names(other, [1]) == {}, (
                "the writer committed on its own; the worker's boundary moved")

        writer.commit()

    with engine.connect() as other:
        assert assets_api.load_cached_container_names(other, [1]) == {1: "Ore Can"}
