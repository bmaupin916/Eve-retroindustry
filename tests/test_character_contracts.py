"""`app/character/contracts.py`, before it moves onto the portable query layer.

Nine of its functions have no test at all: `type_label`, `status_label`,
`_auth`, `_get_all_pages`, `fetch_corp_contracts`, `fetch_corp_contract_items`,
`_fetch_public_page`, `fetch_public_contracts` and
`fetch_public_contract_items`. `tests/test_orders_cache.py` covers the four
cache functions and two failure paths, and those assertions stay where they are.

**Scope.** This file covers the module's database surface and the fetchers that
write through it — what the conversion can break. The public-contract functions
(`_fetch_public_page`, `fetch_public_contracts`,
`fetch_public_contract_items`) touch no database at all, so a conversion cannot
reach them; they are a real coverage gap but a separate one, and testing them
here would be scope disguised as diligence. Same for the two label maps.

**These assertions are written against the `sqlite3` version on purpose.** They
have to exist before the rewrite so the rewrite can be judged by whether it
preserves them.

Four things here are conversion traps rather than ordinary behaviour:

* **Both writers are upserts, not DELETE-then-INSERT.** `ON CONFLICT (...) DO
  UPDATE SET ... excluded.x` is the one upsert spelling SQLite and Postgres
  share, and `contracts_cache` conflicts on a **composite** key
  `(owner_id, owner_kind)`. Get the conflict target wrong and the failure is
  not an error — it is a second row, and one owner silently having two
  contract lists.
* **Neither writer commits.** The caller owns the transaction boundary, exactly
  as with `save_cached_container_names`. A conversion that adds a commit inside
  the writer moves that boundary; one that drops the caller's loses the write.
* **`_get_all_pages` distinguishes "ESI is down" from "no contracts".** Page one
  failing returns `None`; a *later* page failing returns what already arrived.
  That is what stops a transport blip being cached as an empty contract list —
  and `fetch_character_contracts` only writes the cache when it is not `None`.
* **`contract_items_cache` has no expiry and nothing refreshes it.** Caching
  `[]` after a failed expand would show that contract as empty permanently, so
  the failure paths must not write.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app.character import contracts as contracts_api
from app.db.schema import apply_schema

ALICE = 2_112_625_428
CORP = 98_000_001
CONTRACT = 234_567_890
BIG_CONTRACT = 9_223_372_036_854_775_000   # a real int64, well past 2**31


# ── stubs ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, payload=None, pages: int = 1):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = {"x-pages": str(pages)}

    def json(self):
        return self._payload


class _Client:
    """Enough of httpx.AsyncClient for these fetchers.

    A queued `Exception` is raised rather than returned, which is how the
    transport-failure paths are reached — those are `except Exception`, not
    status codes.
    """

    def __init__(self, *responses):
        self._queue = list(responses)
        self.urls: list[str] = []
        self.params: list[dict] = []
        self.headers: list[dict] = []

    async def get(self, url, **kw):
        self.urls.append(url)
        self.params.append(kw.get("params") or {})
        self.headers.append(kw.get("headers") or {})
        nxt = self._queue.pop(0) if self._queue else _Resp(500)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def calls(self) -> int:
        return len(self.urls)


def _contract(cid, type_="item_exchange", status="outstanding"):
    return {"contract_id": cid, "type": type_, "status": status}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "contracts.db"))
    apply_schema(c)
    yield c
    c.close()


def _owner_rows(conn, owner_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM contracts_cache WHERE owner_id=?",
        (owner_id,)).fetchone()[0]


def _item_rows(conn, contract_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM contract_items_cache WHERE contract_id=?",
        (contract_id,)).fetchone()[0]


# ── the contracts cache ──────────────────────────────────────────────────────

def test_contracts_round_trip_with_an_age(conn):
    contracts_api.save_cached_contracts(conn, ALICE, [_contract(1), _contract(2)])
    conn.commit()

    got, cached_at = contracts_api.load_cached_contracts(conn, ALICE)

    assert [c["contract_id"] for c in got] == [1, 2]
    assert cached_at > 0


def test_saving_twice_replaces_rather_than_duplicating(conn):
    """An upsert on the composite key, not DELETE-then-INSERT. If the conflict
    target were wrong the second save would insert instead of update, and the
    owner would have two contract lists with nothing to choose between them."""
    contracts_api.save_cached_contracts(conn, ALICE, [_contract(1)])
    contracts_api.save_cached_contracts(conn, ALICE, [_contract(2)])
    conn.commit()

    assert _owner_rows(conn, ALICE) == 1
    got, _ = contracts_api.load_cached_contracts(conn, ALICE)
    assert [c["contract_id"] for c in got] == [2], "the older save won"


def test_the_second_save_moves_the_age_forward(conn):
    """`cached_at=excluded.cached_at` in the DO UPDATE. Without it the row
    keeps its first timestamp and the page reports fresh data as old."""
    contracts_api.save_cached_contracts(conn, ALICE, [_contract(1)])
    conn.commit()
    _, first = contracts_api.load_cached_contracts(conn, ALICE)

    conn.execute("UPDATE contracts_cache SET cached_at=? WHERE owner_id=?",
                 (first - 3600, ALICE))
    conn.commit()

    contracts_api.save_cached_contracts(conn, ALICE, [_contract(2)])
    conn.commit()

    _, second = contracts_api.load_cached_contracts(conn, ALICE)
    assert second > first - 3600, "the upsert kept the old timestamp"


def test_owner_kind_is_part_of_the_key(conn):
    """A character id and a corporation id can collide. `owner_kind` is what
    keeps them apart, and it is half the conflict target."""
    contracts_api.save_cached_contracts(conn, CORP, [_contract(1)])
    contracts_api.save_cached_contracts(conn, CORP, [_contract(2)],
                                        contracts_api.CORPORATION)
    conn.commit()

    assert _owner_rows(conn, CORP) == 2, "one row — the two kinds collapsed"
    personal, _ = contracts_api.load_cached_contracts(conn, CORP)
    corp, _ = contracts_api.load_cached_contracts(
        conn, CORP, contracts_api.CORPORATION)
    assert [c["contract_id"] for c in personal] == [1]
    assert [c["contract_id"] for c in corp] == [2]


def test_the_writer_does_not_commit(conn, tmp_path):
    """The caller owns the transaction boundary. A separate connection must not
    see the row until that caller commits."""
    contracts_api.save_cached_contracts(conn, ALICE, [_contract(1)])

    other = sqlite3.connect(str(tmp_path / "contracts.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM contracts_cache WHERE owner_id=?",
            (ALICE,)).fetchone()[0] == 0, (
            "the writer committed — the caller's boundary moved into it")
    finally:
        other.close()

    conn.commit()
    other = sqlite3.connect(str(tmp_path / "contracts.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM contracts_cache WHERE owner_id=?",
            (ALICE,)).fetchone()[0] == 1, "the caller's commit did not land"
    finally:
        other.close()


def test_an_empty_contract_list_is_still_a_sync(conn):
    """`[]` means "looked, has none"; `None` means "never looked". A page that
    confuses them tells you a courier contract expired safely."""
    contracts_api.save_cached_contracts(conn, ALICE, [])
    conn.commit()

    got, cached_at = contracts_api.load_cached_contracts(conn, ALICE)
    assert got == []
    assert cached_at > 0


def test_a_corrupt_contracts_cache_reads_as_never_synced(conn):
    conn.execute(
        "INSERT INTO contracts_cache (owner_id, owner_kind, data_json, cached_at)"
        " VALUES (?,?,?,?)", (ALICE, contracts_api.CHARACTER, "{not json", time.time()))
    conn.commit()

    assert contracts_api.load_cached_contracts(conn, ALICE) == (None, 0.0)


def test_cached_at_cannot_be_null(conn):
    """`load_cached_contracts` coalesces with `float(row[1] or 0.0)`, which
    reads like a live case and is not one: `contracts_cache.cached_at` is
    `NOT NULL`, unlike `char_blueprints_cache.cached_at` beside it. The
    coalesce is belt-and-braces, and *this* is the assertion that holds — if
    the constraint were ever dropped, an age of `None` would reach a page.
    """
    contracts_api.save_cached_contracts(conn, ALICE, [_contract(1)])
    conn.commit()

    with pytest.raises(Exception) as exc:
        conn.execute("UPDATE contracts_cache SET cached_at=NULL WHERE owner_id=?",
                     (ALICE,))
    assert "NOT NULL" in str(exc.value).upper() or "null value" in str(exc.value)


# ── the contract-items cache ─────────────────────────────────────────────────

def test_contract_items_round_trip_without_an_age(conn):
    """The one cache here that cannot go stale — a contract's contents are
    fixed at creation — so it returns items alone. Offering a timestamp would
    invite a staleness check that means nothing."""
    assert contracts_api.load_cached_contract_items(conn, CONTRACT) is None

    contracts_api.save_cached_contract_items(conn, CONTRACT, [{"type_id": 34}])
    conn.commit()

    assert [i["type_id"] for i in
            contracts_api.load_cached_contract_items(conn, CONTRACT)] == [34]


def test_saving_items_twice_replaces_rather_than_duplicating(conn):
    contracts_api.save_cached_contract_items(conn, CONTRACT, [{"type_id": 34}])
    contracts_api.save_cached_contract_items(conn, CONTRACT, [{"type_id": 35}])
    conn.commit()

    assert _item_rows(conn, CONTRACT) == 1
    assert [i["type_id"] for i in
            contracts_api.load_cached_contract_items(conn, CONTRACT)] == [35]


def test_the_items_writer_does_not_commit(conn, tmp_path):
    contracts_api.save_cached_contract_items(conn, CONTRACT, [{"type_id": 34}])

    other = sqlite3.connect(str(tmp_path / "contracts.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM contract_items_cache WHERE contract_id=?",
            (CONTRACT,)).fetchone()[0] == 0
    finally:
        other.close()


def test_an_empty_item_list_is_distinguishable_from_never_expanded(conn):
    """A courier contract really can hold nothing. `None` still has to mean
    "not fetched", because nothing here ever refreshes."""
    contracts_api.save_cached_contract_items(conn, CONTRACT, [])
    conn.commit()

    assert contracts_api.load_cached_contract_items(conn, CONTRACT) == []


def test_a_corrupt_items_cache_reads_as_never_fetched(conn):
    conn.execute(
        "INSERT INTO contract_items_cache (contract_id, data_json, cached_at)"
        " VALUES (?,?,?)", (CONTRACT, "{not json", time.time()))
    conn.commit()

    assert contracts_api.load_cached_contract_items(conn, CONTRACT) is None


def test_an_int64_contract_id_survives(conn):
    """Contract ids are int64 and the big ones are real. A truncating column
    would collide two contracts onto one cache row."""
    contracts_api.save_cached_contract_items(conn, BIG_CONTRACT, [{"type_id": 34}])
    contracts_api.save_cached_contract_items(conn, BIG_CONTRACT - 1, [{"type_id": 35}])
    conn.commit()

    assert [i["type_id"] for i in
            contracts_api.load_cached_contract_items(conn, BIG_CONTRACT)] == [34]
    assert [i["type_id"] for i in
            contracts_api.load_cached_contract_items(conn, BIG_CONTRACT - 1)] == [35]


def test_an_int64_owner_id_survives(conn):
    contracts_api.save_cached_contracts(conn, BIG_CONTRACT, [_contract(1)])
    conn.commit()

    got, _ = contracts_api.load_cached_contracts(conn, BIG_CONTRACT)
    assert [c["contract_id"] for c in got] == [1]


# ── _get_all_pages: "ESI is down" is not "no contracts" ──────────────────────

def test_a_first_page_error_status_is_none_not_empty(conn):
    got = asyncio.run(contracts_api._get_all_pages(
        _Client(_Resp(500)), "http://x/", "tok"))

    assert got is None, "an unavailable endpoint reported as zero contracts"


def test_a_first_page_transport_failure_is_none_not_empty(conn):
    got = asyncio.run(contracts_api._get_all_pages(
        _Client(RuntimeError("connection reset")), "http://x/", "tok"))

    assert got is None


def test_a_later_page_failing_keeps_what_arrived(conn):
    """Deliberately different from page one. Something is better than nothing
    once we know the character *has* contracts, and `None` would throw away a
    page we already hold."""
    client = _Client(_Resp(200, [_contract(1)], pages=3), _Resp(500))

    got = asyncio.run(contracts_api._get_all_pages(client, "http://x/", "tok"))

    assert [c["contract_id"] for c in got] == [1]


def test_a_later_page_transport_failure_keeps_what_arrived(conn):
    client = _Client(_Resp(200, [_contract(1)], pages=3),
                     RuntimeError("connection reset"))

    got = asyncio.run(contracts_api._get_all_pages(client, "http://x/", "tok"))

    assert [c["contract_id"] for c in got] == [1]


def test_every_page_is_walked(conn):
    client = _Client(_Resp(200, [_contract(1)], pages=3),
                     _Resp(200, [_contract(2)], pages=3),
                     _Resp(200, [_contract(3)], pages=3))

    got = asyncio.run(contracts_api._get_all_pages(client, "http://x/", "tok"))

    assert [c["contract_id"] for c in got] == [1, 2, 3]
    assert [p.get("page") for p in client.params] == [1, 2, 3]


def test_an_empty_page_stops_the_walk(conn):
    """ESI can report more pages than it fills. Without this the loop keeps
    asking for pages that answer nothing, up to max_pages."""
    client = _Client(_Resp(200, [_contract(1)], pages=30), _Resp(200, [], pages=30))

    got = asyncio.run(contracts_api._get_all_pages(client, "http://x/", "tok"))

    assert [c["contract_id"] for c in got] == [1]
    assert client.calls == 2


def test_max_pages_caps_the_walk(conn):
    """A guard against an x-pages that never lets us stop."""
    client = _Client(*[_Resp(200, [_contract(i)], pages=99) for i in range(10)])

    got = asyncio.run(contracts_api._get_all_pages(
        client, "http://x/", "tok", max_pages=3))

    assert client.calls == 3
    assert len(got) == 3


def test_the_default_cap_is_the_one_that_actually_applies(conn):
    """Passing `max_pages` explicitly says nothing about the default, and the
    default is the live value — `fetch_character_contracts` never passes one.
    A mutation that raised it from 30 to 10,000 went unnoticed until this
    existed, because the test above pinned an argument no caller supplies.
    """
    client = _Client(*[_Resp(200, [_contract(i)], pages=9999) for i in range(40)])

    got = asyncio.run(contracts_api._get_all_pages(client, "http://x/", "tok"))

    assert client.calls == 30
    assert len(got) == 30


def test_the_token_is_sent_when_there_is_one(conn):
    client = _Client(_Resp(200, []))

    asyncio.run(contracts_api._get_all_pages(client, "http://x/", "tok-abc"))

    assert client.headers[0].get("Authorization") == "Bearer tok-abc"


def test_no_token_means_no_authorization_header(conn):
    """The public endpoints go through here too, and sending an empty bearer
    is worse than sending nothing."""
    client = _Client(_Resp(200, []))

    asyncio.run(contracts_api._get_all_pages(client, "http://x/", None))

    assert "Authorization" not in client.headers[0]


# ── the character fetcher ────────────────────────────────────────────────────

def test_a_successful_character_fetch_is_cached(conn):
    client = _Client(_Resp(200, [_contract(1)]))

    got = asyncio.run(contracts_api.fetch_character_contracts(
        client, ALICE, "tok", conn=conn))
    conn.commit()

    assert [c["contract_id"] for c in got] == [1]
    cached, _ = contracts_api.load_cached_contracts(conn, ALICE)
    assert [c["contract_id"] for c in cached] == [1]


def test_a_failed_character_fetch_does_not_touch_the_cache(conn):
    """`out is not None` guards the save. Without it a transport blip
    overwrites a good list with an empty one."""
    contracts_api.save_cached_contracts(conn, ALICE, [_contract(1)])
    conn.commit()

    got = asyncio.run(contracts_api.fetch_character_contracts(
        _Client(_Resp(500)), ALICE, "tok", conn=conn))
    conn.commit()

    assert got is None
    kept, _ = contracts_api.load_cached_contracts(conn, ALICE)
    assert [c["contract_id"] for c in kept] == [1]


def test_a_character_fetch_without_a_connection_still_returns(conn):
    """`conn` is optional — the page can fetch without caching."""
    got = asyncio.run(contracts_api.fetch_character_contracts(
        _Client(_Resp(200, [_contract(1)])), ALICE, "tok"))

    assert [c["contract_id"] for c in got] == [1]
    assert contracts_api.load_cached_contracts(conn, ALICE) == (None, 0.0)


def test_a_character_fetch_caches_under_the_character_kind(conn):
    asyncio.run(contracts_api.fetch_character_contracts(
        _Client(_Resp(200, [_contract(1)])), ALICE, "tok", conn=conn))
    conn.commit()

    assert contracts_api.load_cached_contracts(
        conn, ALICE, contracts_api.CORPORATION) == (None, 0.0)


# ── the corporation fetcher, which answers with a reason ─────────────────────

def test_a_successful_corp_fetch_is_cached_under_the_corporation_kind(conn):
    client = _Client(_Resp(200, [_contract(1)]))

    got, err = asyncio.run(contracts_api.fetch_corp_contracts(
        client, CORP, "tok", conn=conn))
    conn.commit()

    assert err is None
    assert [c["contract_id"] for c in got] == [1]
    cached, _ = contracts_api.load_cached_contracts(
        conn, CORP, contracts_api.CORPORATION)
    assert [c["contract_id"] for c in cached] == [1]
    assert contracts_api.load_cached_contracts(conn, CORP) == (None, 0.0), (
        "the corporation's contracts were filed under the character kind")


def test_a_403_names_the_missing_role(conn):
    """The single most common failure here, and the only one the user can fix.
    "ESI returned HTTP 403" would send them to look for an outage."""
    got, err = asyncio.run(contracts_api.fetch_corp_contracts(
        _Client(_Resp(403)), CORP, "tok", conn=conn))

    assert got is None
    assert "Accountant" in err


def test_another_error_status_is_reported_with_its_code(conn):
    got, err = asyncio.run(contracts_api.fetch_corp_contracts(
        _Client(_Resp(503)), CORP, "tok", conn=conn))

    assert got is None
    assert "503" in err


def test_a_corp_transport_failure_is_reported_not_swallowed(conn):
    got, err = asyncio.run(contracts_api.fetch_corp_contracts(
        _Client(RuntimeError("connection reset")), CORP, "tok", conn=conn))

    assert got is None
    assert "connection reset" in err


def test_a_failed_corp_fetch_does_not_touch_the_cache(conn):
    contracts_api.save_cached_contracts(conn, CORP, [_contract(1)],
                                        contracts_api.CORPORATION)
    conn.commit()

    asyncio.run(contracts_api.fetch_corp_contracts(
        _Client(_Resp(403)), CORP, "tok", conn=conn))
    conn.commit()

    kept, _ = contracts_api.load_cached_contracts(
        conn, CORP, contracts_api.CORPORATION)
    assert [c["contract_id"] for c in kept] == [1]


def test_the_corp_fetcher_walks_its_own_pages(conn):
    """It does not go through `_get_all_pages` — it needs the first response's
    status code to tell a missing role from an outage, so it paginates itself."""
    client = _Client(_Resp(200, [_contract(1)], pages=3),
                     _Resp(200, [_contract(2)], pages=3),
                     _Resp(200, [_contract(3)], pages=3))

    got, err = asyncio.run(contracts_api.fetch_corp_contracts(
        client, CORP, "tok", conn=conn))

    assert err is None
    assert [c["contract_id"] for c in got] == [1, 2, 3]


def test_a_later_corp_page_failing_keeps_what_arrived(conn):
    client = _Client(_Resp(200, [_contract(1)], pages=3), _Resp(500))

    got, err = asyncio.run(contracts_api.fetch_corp_contracts(
        client, CORP, "tok", conn=conn))

    assert err is None
    assert [c["contract_id"] for c in got] == [1]


# ── the item fetchers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("fetcher", ["fetch_character_contract_items",
                                     "fetch_corp_contract_items"])
def test_a_successful_item_fetch_is_cached(conn, fetcher):
    got = asyncio.run(getattr(contracts_api, fetcher)(
        _Client(_Resp(200, [{"type_id": 34}])), ALICE, CONTRACT, "tok", conn=conn))
    conn.commit()

    assert [i["type_id"] for i in got] == [34]
    assert [i["type_id"] for i in
            contracts_api.load_cached_contract_items(conn, CONTRACT)] == [34]


@pytest.mark.parametrize("fetcher", ["fetch_character_contract_items",
                                     "fetch_corp_contract_items"])
@pytest.mark.parametrize("failure", [_Resp(500), RuntimeError("reset")],
                         ids=["error-status", "transport"])
def test_a_failed_item_fetch_is_never_cached(conn, fetcher, failure):
    """Nothing refreshes this cache — no TTL, and the worker does not prefetch
    it. An `[]` written here is permanent, and the contract shows as empty for
    as long as the database lives."""
    got = asyncio.run(getattr(contracts_api, fetcher)(
        _Client(failure), ALICE, CONTRACT, "tok", conn=conn))
    conn.commit()

    assert got is None
    assert contracts_api.load_cached_contract_items(conn, CONTRACT) is None


@pytest.mark.parametrize("fetcher", ["fetch_character_contract_items",
                                     "fetch_corp_contract_items"])
def test_an_item_fetch_without_a_connection_still_returns(conn, fetcher):
    got = asyncio.run(getattr(contracts_api, fetcher)(
        _Client(_Resp(200, [{"type_id": 34}])), ALICE, CONTRACT, "tok"))

    assert [i["type_id"] for i in got] == [34]
    assert contracts_api.load_cached_contract_items(conn, CONTRACT) is None
