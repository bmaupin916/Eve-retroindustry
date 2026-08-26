"""`app/character/blueprints.py`, before it moves onto the portable query layer.

The coverage probe finds three of its functions never executed by the suite:
`_save_cache` and `_load_cache` — the cache writer among
them. `fetch_blueprints` shows as dead too, but that is the probe's known blind
spot: `tests/test_sync_worker.py` monkeypatches it onto the *worker* module, so
the worker is well covered while the real function never runs. It has no tests
here either way, which is what matters.

`load_cached_blueprints` is the one exception — `tests/test_orders_cache.py`
pins that it ignores the TTL and that an unsynced character reads as `None`.
Those two assertions stay where they are; everything below is new.

**These assertions are unchanged by the conversion.** They were written against
the `sqlite3` version first, so the rewrite could be judged by whether it
preserves them rather than assembled first and described afterwards. Only the
fixture underneath moved, and it now runs every one of them on both backends.
The one exception used to be a SQLite-only test of `ensure_bp_table`; that shim
had no caller left in `app/` and went in v0.9.76 with five siblings in the same
state, so there is no exception any more.

Four things here are conversion traps rather than ordinary behaviour:

* **`char_blueprints_cache` has no primary key and no `UNIQUE(character_id)`.**
  That is why `_save_cache` is DELETE-then-INSERT rather than an upsert. Drop
  the DELETE and a second save leaves two rows, `fetchone()` picks whichever
  the backend feels like returning, and SQLite and Postgres need not agree.
* **Two readers of one table with opposite TTL rules.** `_load_cache` enforces
  `CACHE_TTL` because the fetcher uses it to decide on a round trip;
  `load_cached_blueprints` ignores it because a page must render from whatever
  is there. A conversion that unifies them breaks one caller or the other.
* **A corrupt cache reads as never-synced, not as empty.**
  `load_cached_blueprints` catches `ValueError`/`TypeError`/`KeyError` and
  returns `(None, 0.0)`. `None` and `[]` mean different things everywhere in
  this codebase, and this is the path that decides which one bad JSON gets.
  Writing that test found a hole in it: a payload that parses but is not a
  list of dicts raised `AttributeError`, which the tuple did not catch, so it
  escaped to the page as a 500. **Fixed in v0.9.76** — see
  `test_a_non_dict_entry_reads_as_never_synced`, which was written asserting the
  crash so the fix had to be a deliberate commit rather than a quiet one.
* **`_save_cache` commits; `fetch_blueprints` does not commit separately.** The
  transaction boundary lives inside the writer. Moving it is invisible until a
  caller rolls back.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from sqlalchemy import create_engine, text

from app.character import blueprints as bp_api
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_character_blueprints"

CHAR = 2_112_625_428
JITA = 60003760
STRUCTURE = 1_049_982_731_184
RAVEN_BP = 692


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
    """Enough of httpx.AsyncClient for this fetcher."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.urls: list[str] = []
        self.params: list[dict] = []

    async def get(self, url, **kw):
        self.urls.append(url)
        self.params.append(kw.get("params") or {})
        nxt = self._queue.pop(0) if self._queue else _Resp(500)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def calls(self) -> int:
        return len(self.urls)


def _bp(item_id, type_id=RAVEN_BP, location_id=JITA, quantity=-1,
        me=10, te=20, runs=-1, flag="Hangar"):
    return {"item_id": item_id, "type_id": type_id, "location_id": location_id,
            "location_flag": flag, "quantity": quantity,
            "material_efficiency": me, "time_efficiency": te, "runs": runs}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    """An engine per backend, built once for the module.

    Function scope here would drop and rebuild a Postgres schema — every
    migration — for each test in the file; clearing the one table between tests
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


@pytest.fixture(autouse=True)
def _empty_table(engine):
    """Before, not after: a test that dies half-way must not leave its row for
    the next one to read."""
    with engine.connect() as c:
        c.execute(text("DELETE FROM char_blueprints_cache"))
        c.commit()
    yield


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _backend(conn) -> str:
    return conn.engine.dialect.name


def _rows(conn) -> int:
    return conn.execute(
        text("SELECT COUNT(*) FROM char_blueprints_cache WHERE character_id=:cid"),
        {"cid": CHAR}).fetchone()[0]


def _age_to(conn, when: float | None) -> None:
    """Backdate the cached row, or NULL it. `cached_at` is nullable, and both
    readers coalesce it, so `None` here is the "infinitely old" case rather
    than a separate one."""
    conn.execute(
        text("UPDATE char_blueprints_cache SET cached_at=:at"
             " WHERE character_id=:cid"), {"at": when, "cid": CHAR})
    conn.commit()


def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing file: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    assert conn.execute(
        text("SELECT COUNT(*) FROM char_blueprints_cache")).fetchone()[0] == 0


# ── the schema shim ──────────────────────────────────────────────────────────

# ── the cache writer ─────────────────────────────────────────────────────────

def test_saved_blueprints_read_back(conn):
    bp_api._save_cache(conn, CHAR, [_bp(1), _bp(2)])

    got, cached_at = bp_api.load_cached_blueprints(conn, CHAR)

    assert [b.item_id for b in got] == [1, 2]
    assert cached_at > 0


def test_saving_twice_replaces_rather_than_duplicating(conn):
    """DELETE-then-INSERT, because the table has no UNIQUE(character_id) to
    upsert against. Without the DELETE two rows survive for one character and
    `fetchone()` returns whichever the backend happens to hand back first."""
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    bp_api._save_cache(conn, CHAR, [_bp(2)])

    assert _rows(conn) == 1
    got, _ = bp_api.load_cached_blueprints(conn, CHAR)
    assert [b.item_id for b in got] == [2], "the older save won"


def test_saving_commits(engine):
    """The writer owns the transaction boundary. A **separate** connection sees
    the row only if the commit really happened — asking the same connection
    would see its own uncommitted work and prove nothing."""
    with engine.connect() as writer:
        bp_api._save_cache(writer, CHAR, [_bp(1)])

    with engine.connect() as reader:
        assert reader.execute(
            text("SELECT COUNT(*) FROM char_blueprints_cache"
                 " WHERE character_id=:cid"), {"cid": CHAR}).fetchone()[0] == 1


def test_one_characters_cache_does_not_disturb_anothers(conn):
    """The DELETE is scoped by character_id. Unscoped, a second character
    signing in would silently empty the first one's page."""
    other = CHAR + 1
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    bp_api._save_cache(conn, other, [_bp(2)])

    got, _ = bp_api.load_cached_blueprints(conn, CHAR)
    assert [b.item_id for b in got] == [1]


def test_an_empty_blueprint_list_is_still_a_sync(conn):
    """`[]` means "looked, owns none"; `None` means "never looked". Storing
    nothing for an empty result would collapse the two."""
    bp_api._save_cache(conn, CHAR, [])

    assert _rows(conn) == 1
    got, cached_at = bp_api.load_cached_blueprints(conn, CHAR)
    assert got == []
    assert cached_at > 0


# ── the two readers disagree about the TTL, on purpose ───────────────────────

def test_the_fetchers_reader_enforces_the_ttl(conn):
    stale = time.time() - bp_api.CACHE_TTL - 3600
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    _age_to(conn, stale)

    assert bp_api._load_cache(conn, CHAR) is None


def test_the_fetchers_reader_returns_a_fresh_cache(conn):
    bp_api._save_cache(conn, CHAR, [_bp(1)])

    cached = bp_api._load_cache(conn, CHAR)

    assert cached is not None
    assert [item["item_id"] for item in cached] == [1]


def test_the_page_reader_ignores_the_ttl(conn):
    """Same split as the assets reader beside it: the TTL answers "is another
    round trip worth it", which is the fetcher's question and not a page's."""
    stale = time.time() - bp_api.CACHE_TTL - 3600
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    _age_to(conn, stale)

    assert bp_api._load_cache(conn, CHAR) is None, (
        "the row is not actually stale — this test would pass vacuously")

    got, cached_at = bp_api.load_cached_blueprints(conn, CHAR)

    assert got is not None and [b.item_id for b in got] == [1]
    assert cached_at == pytest.approx(stale)


def test_a_null_cached_at_is_treated_as_the_epoch(conn):
    """`cached_at` is nullable. `time.time() - None` would raise; both readers
    coalesce it, and a NULL therefore reads as infinitely old."""
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    _age_to(conn, None)

    assert bp_api._load_cache(conn, CHAR) is None

    got, cached_at = bp_api.load_cached_blueprints(conn, CHAR)
    assert [b.item_id for b in got] == [1]
    assert cached_at == 0.0


def test_an_unsynced_character_reads_as_none_not_empty(conn):
    assert bp_api._load_cache(conn, CHAR) is None
    assert bp_api.load_cached_blueprints(conn, CHAR) == (None, 0.0)


# ── a corrupt cache reads as never-synced ────────────────────────────────────

def _corrupt(conn, payload: str) -> None:
    """Write a payload `_save_cache` would never produce — that is the point:
    these are the shapes the reader has to survive, not the ones it writes."""
    conn.execute(
        text("INSERT INTO char_blueprints_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :cached_at)"),
        {"cid": CHAR, "data": payload, "cached_at": time.time()})
    conn.commit()


def test_unparseable_json_reads_as_never_synced(conn):
    """`(None, 0.0)`, not `([], 0.0)`. A page told "you own no blueprints" acts
    on that; a page told "never synced" offers to sync. Corrupt data must land
    on the second."""
    _corrupt(conn, "not json at all")

    assert bp_api.load_cached_blueprints(conn, CHAR) == (None, 0.0)


#: Read as `item[...]` with no default, so each one missing is a KeyError.
REQUIRED = ("item_id", "type_id", "location_id")


@pytest.mark.parametrize("missing", REQUIRED)
def test_an_entry_missing_a_required_key_reads_as_never_synced(conn, missing):
    """One case per required key, dropped **on its own**.

    A single payload missing several keys proves much less than it looks: with
    `item_id` given a default the entry still raised on `location_id`, so a
    mutation that stopped requiring `item_id` went unnoticed. Each key has to
    be the only thing wrong to show that each is really required.
    """
    entry = _bp(1)
    del entry[missing]
    _corrupt(conn, json.dumps([entry]))

    assert bp_api.load_cached_blueprints(conn, CHAR) == (None, 0.0)


@pytest.mark.parametrize("payload, shape", [
    ('{"item_id": 1}', "an object where a list belongs — iterating it yields keys"),
    ('[null]', "a list holding null"),
    ('[1, 2]', "a list of bare numbers"),
])
def test_a_non_dict_entry_reads_as_never_synced(conn, payload, shape):
    """**Fixed in v0.9.76. This test used to assert the crash.**

    `_parse_blueprints` reaches straight for `item.get(...)`, so anything in the
    list that is not a dict raises `AttributeError` — and the handler caught only
    `ValueError`/`TypeError`/`KeyError`. The exception escaped to `/assets`,
    `/blueprints` and `/plan` as a **500** instead of reading as never-synced
    like every other corrupt payload.

    The test was written asserting `pytest.raises(AttributeError)` on purpose,
    so that widening the tuple had to be a deliberate commit rather than
    something smuggled into the query conversion. This is that commit, and this
    is the assertion flipping.

    **What made it this reader and not the assets one beside it is one line's
    ordering.** `_parse_assets` opens with `item["item_id"]`, so a non-dict
    raises `TypeError` and is caught; `_parse_blueprints` opens with
    `item.get("quantity", -1)`, so the identical payload raises `AttributeError`
    and was not. The two functions are otherwise the same. Both readers now
    catch both, so the guarantee no longer depends on which access happens to
    come first — an ordinary reorder of those fields would have reintroduced it.
    """
    _corrupt(conn, payload)

    assert bp_api.load_cached_blueprints(conn, CHAR) == (None, 0.0), shape


# ── parsing ──────────────────────────────────────────────────────────────────

def test_quantity_minus_one_is_an_original(conn):
    bp_api._save_cache(conn, CHAR, [_bp(1, quantity=-1, runs=-1)])

    (got,), _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert got.is_original is True
    assert got.runs == -1


def test_quantity_minus_two_is_a_copy(conn):
    """A BPC. Anything that is not exactly -1 is a copy, and getting this
    backwards would price a 10-run copy as an unlimited original."""
    bp_api._save_cache(conn, CHAR, [_bp(1, quantity=-2, runs=10)])

    (got,), _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert got.is_original is False
    assert got.runs == 10


def test_a_stacked_quantity_is_not_an_original(conn):
    """ESI reports a positive quantity for stacked copies. Only -1 is a BPO."""
    bp_api._save_cache(conn, CHAR, [_bp(1, quantity=5, runs=3)])

    (got,), _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert got.is_original is False


def test_me_and_te_survive_the_round_trip(conn):
    """These two drive every material and time number downstream. A blueprint
    that reads back as ME 0 when it is ME 10 overstates the build by 10%."""
    bp_api._save_cache(conn, CHAR, [_bp(1, me=10, te=20)])

    (got,), _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert got.material_efficiency == 10
    assert got.time_efficiency == 20


def test_missing_optional_fields_take_their_defaults(conn):
    """ESI omits `runs`, `material_efficiency`, `time_efficiency` and
    `location_flag` for some entries. Only `item_id`, `type_id` and
    `location_id` are read without a default."""
    bp_api._save_cache(conn, CHAR, [{
        "item_id": 1, "type_id": RAVEN_BP, "location_id": JITA, "quantity": -1,
    }])

    (got,), _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert got.location_flag == "Hangar"
    assert got.runs == -1
    assert got.material_efficiency == 0
    assert got.time_efficiency == 0


def test_a_missing_quantity_defaults_to_an_original(conn):
    bp_api._save_cache(conn, CHAR, [{
        "item_id": 1, "type_id": RAVEN_BP, "location_id": JITA,
    }])

    (got,), _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert got.is_original is True


def test_a_structure_location_id_survives(conn):
    """Player-owned structures have int64 ids. This is the column that already
    had to be widened for `station_rigs`; here it rides inside JSON, but the
    value still has to come back whole."""
    bp_api._save_cache(conn, CHAR, [_bp(1, location_id=STRUCTURE)])

    (got,), _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert got.location_id == STRUCTURE


def test_order_is_preserved(conn):
    """The page renders in the order the list arrives in."""
    bp_api._save_cache(conn, CHAR, [_bp(3), _bp(1), _bp(2)])

    got, _ = bp_api.load_cached_blueprints(conn, CHAR)

    assert [b.item_id for b in got] == [3, 1, 2]


# ── the fetcher ──────────────────────────────────────────────────────────────

def test_fetch_serves_a_fresh_cache_without_calling_esi(conn):
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    client = _Client()

    got = asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    assert client.calls == 0, "a fresh cache still cost an ESI call"
    assert [b.item_id for b in got] == [1]


def test_fetch_goes_to_esi_when_the_cache_is_stale(conn):
    stale = time.time() - bp_api.CACHE_TTL - 3600
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    _age_to(conn, stale)
    client = _Client(_Resp(200, [_bp(2)]))

    got = asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    assert client.calls == 1
    assert [b.item_id for b in got] == [2]


def test_force_refresh_ignores_a_fresh_cache(conn):
    """The manual "sync now" path. Without this the button does nothing for
    fifteen minutes and reads as broken."""
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    client = _Client(_Resp(200, [_bp(2)]))

    got = asyncio.run(
        bp_api.fetch_blueprints(client, CHAR, "tok", conn, force_refresh=True))

    assert client.calls == 1
    assert [b.item_id for b in got] == [2]


def test_fetch_stores_what_it_fetched(conn):
    client = _Client(_Resp(200, [_bp(1), _bp(2)]))

    asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    assert _rows(conn) == 1
    cached, _ = bp_api.load_cached_blueprints(conn, CHAR)
    assert [b.item_id for b in cached] == [1, 2]


def test_fetch_sends_the_access_token(conn):
    """Blueprints are an authenticated endpoint; without the header ESI
    answers 403 and the page shows an empty hangar."""
    seen = {}

    class _Recording(_Client):
        async def get(self, url, **kw):
            seen.update(kw.get("headers") or {})
            return await super().get(url, **kw)

    asyncio.run(bp_api.fetch_blueprints(
        _Recording(_Resp(200, [])), CHAR, "tok-abc", conn))

    assert seen.get("Authorization") == "Bearer tok-abc"


def test_fetch_walks_every_page(conn):
    """`x-pages` on the first response is the page count. Stopping at one
    silently truncates a large account's blueprint list."""
    client = _Client(_Resp(200, [_bp(1)], pages=3),
                     _Resp(200, [_bp(2)], pages=3),
                     _Resp(200, [_bp(3)], pages=3))

    got = asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    assert client.calls == 3
    assert [p.get("page") for p in client.params] == [1, 2, 3]
    assert [b.item_id for b in got] == [1, 2, 3]


def test_fetch_stops_after_one_page_when_there_is_only_one(conn):
    client = _Client(_Resp(200, [_bp(1)], pages=1))

    asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    assert client.calls == 1


def test_a_missing_x_pages_header_means_one_page(conn):
    """Not every ESI response carries it. Defaulting to anything above 1 would
    request a page that does not exist."""
    resp = _Resp(200, [_bp(1)])
    del resp.headers["x-pages"]
    client = _Client(resp)

    asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    assert client.calls == 1


def test_an_esi_error_raises_and_leaves_the_cache_alone(conn):
    """`raise_for_status`. A failed sync must not overwrite good data with
    nothing — an empty blueprint list would read as "you own none"."""
    bp_api._save_cache(conn, CHAR, [_bp(1)])
    _age_to(conn, time.time() - bp_api.CACHE_TTL - 3600)
    client = _Client(_Resp(500))

    with pytest.raises(RuntimeError):
        asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    got, _ = bp_api.load_cached_blueprints(conn, CHAR)
    assert [b.item_id for b in got] == [1], "a failed fetch cleared the cache"


def test_fetch_records_an_empty_result_as_a_sync(conn):
    """A character who owns no blueprints must end up at `[]`, not `None`."""
    client = _Client(_Resp(200, []))

    got = asyncio.run(bp_api.fetch_blueprints(client, CHAR, "tok", conn))

    assert got == []
    assert bp_api.load_cached_blueprints(conn, CHAR)[0] == []
