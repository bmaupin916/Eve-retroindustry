"""`/orders` renders from a cache the background worker fills.

The page used to make up to four ESI calls per view — active orders or ninety
days of history, per character or per corporation, plus a lookup for the
character's corporation id on every single load. In "all characters" mode that
multiplied by the number of characters signed in. Each of those spent the
shared error budget, so *looking* at the page made the sync that keeps it
useful more likely to be throttled.

`app/web/routers/industry.py::jobs_page` is the pattern this follows. What it
did not come with is tests — the jobs cache is covered only by the scan in
`test_cache_only_routes.py`, which proves the handler contains no `fetch_`
call and nothing about whether the cache is right. These are the assertions
that scan cannot make.

**The distinction this file exists to defend:** `None` from the cache means
"never synced" and `[]` means "synced, nothing there". A page that shows an
empty order book when it has simply not looked yet is a page that lies, and it
lies in the direction that costs money — you conclude your orders were filled.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from app.character import orders as orders_api
from app.db.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "orders.db"))
    apply_schema(c)
    yield c
    c.close()



def _engine_conn(conn):
    """An engine connection onto the same file this raw `conn` is attached to.

    `app/character/assets.py`, `app/character/blueprints.py` and
    `app/character/contracts.py` are on the portable query layer; the fixture
    above is shared with tests for modules that are not, so it keeps handing
    out a sqlite3 handle and the tests that need the other kind ask for it
    here.
    """
    import contextlib
    from sqlalchemy import create_engine
    path = [r[2] for r in conn.execute("PRAGMA database_list")
            if r[1] == "main"][0]
    eng = create_engine(f"sqlite:///{path}")
    return contextlib.closing(eng.connect())


ALICE, BOB, CORP = 90_000_001, 90_000_002, 98_000_001


def _order(order_id: int, type_id: int = 34) -> dict:
    return {"order_id": order_id, "type_id": type_id, "location_id": 60003760,
            "price": 5.5, "volume_remain": 100, "issued": "2026-08-19T00:00:00Z"}


# ── the distinction ──────────────────────────────────────────────────────────

def test_an_unsynced_owner_reads_as_none_not_empty(conn):
    """The whole reason this returns a tuple rather than a list."""
    orders, cached_at = orders_api.load_cached_orders(conn, ALICE)

    assert orders is None, "never synced must not look like an empty order book"
    assert cached_at == 0.0


def test_a_synced_owner_with_no_orders_reads_as_empty_not_none(conn):
    """The other half. Closing your last order is a real state, and it has to
    be distinguishable from the worker never having run."""
    orders_api.save_cached_orders(conn, ALICE, [])
    conn.commit()

    orders, cached_at = orders_api.load_cached_orders(conn, ALICE)

    assert orders == [], "an empty sync was reported as 'not synced'"
    assert cached_at > 0, "a real reading has a timestamp"


def test_a_corrupt_row_reads_as_unsynced_rather_than_raising(conn):
    """A half-written cache must not take the page down — and it must not read
    as an empty order book either."""
    conn.execute(
        "INSERT INTO market_orders_cache"
        " (owner_id, owner_kind, state, data_json, cached_at) VALUES (?,?,?,?,?)",
        (ALICE, orders_api.CHARACTER, orders_api.ACTIVE, "{not json", 1.0))
    conn.commit()

    orders, _at = orders_api.load_cached_orders(conn, ALICE)

    assert orders is None


# ── the three-part key ───────────────────────────────────────────────────────

def test_the_key_separates_owners(conn):
    """One table holds every owner. A key that collapsed would show one
    character another's orders, which is worse than showing none."""
    orders_api.save_cached_orders(conn, ALICE, [_order(1)])
    orders_api.save_cached_orders(conn, BOB, [_order(2)])
    conn.commit()

    a, _ = orders_api.load_cached_orders(conn, ALICE)
    b, _ = orders_api.load_cached_orders(conn, BOB)

    assert [o["order_id"] for o in a] == [1]
    assert [o["order_id"] for o in b] == [2]


def test_the_key_separates_a_character_from_a_corporation(conn):
    """`owner_kind` is in the key precisely because character and corporation
    ids are drawn from ranges that do not *currently* collide."""
    orders_api.save_cached_orders(conn, CORP, [_order(1)], orders_api.CHARACTER)
    orders_api.save_cached_orders(conn, CORP, [_order(2)], orders_api.CORPORATION)
    conn.commit()

    personal, _ = orders_api.load_cached_orders(conn, CORP, orders_api.CHARACTER)
    corp, _ = orders_api.load_cached_orders(conn, CORP, orders_api.CORPORATION)

    assert [o["order_id"] for o in personal] == [1]
    assert [o["order_id"] for o in corp] == [2], (
        "the corporation's orders were overwritten by the character's")


def test_the_key_separates_active_from_history(conn):
    """Writing ninety days of history must not blank the live order book. This
    is the one that would have bitten: the worker writes both, back to back."""
    orders_api.save_cached_orders(conn, ALICE, [_order(1)],
                                  orders_api.CHARACTER, orders_api.ACTIVE)
    orders_api.save_cached_orders(conn, ALICE, [_order(2), _order(3)],
                                  orders_api.CHARACTER, orders_api.HISTORY)
    conn.commit()

    active, _ = orders_api.load_cached_orders(conn, ALICE, state=orders_api.ACTIVE)
    history, _ = orders_api.load_cached_orders(conn, ALICE, state=orders_api.HISTORY)

    assert [o["order_id"] for o in active] == [1]
    assert [o["order_id"] for o in history] == [2, 3]


def test_a_second_sync_replaces_rather_than_appending(conn):
    """`ON CONFLICT DO UPDATE`. Appending would grow the row without bound and
    show every order you have ever placed as still open."""
    orders_api.save_cached_orders(conn, ALICE, [_order(1)])
    orders_api.save_cached_orders(conn, ALICE, [_order(2)])
    conn.commit()

    rows = conn.execute(
        "SELECT data_json FROM market_orders_cache WHERE owner_id=?", (ALICE,)
    ).fetchall()

    assert len(rows) == 1, f"{len(rows)} rows for one owner/kind/state"
    assert [o["order_id"] for o in json.loads(rows[0][0])] == [2]


# ── failure must not be written down as "no orders" ──────────────────────────

class _Resp:
    def __init__(self, status, payload=None, pages=1):
        self.status_code, self._payload = status, payload
        self.headers = {"x-pages": str(pages)}

    def json(self):
        return self._payload


class _Client:
    """Enough of httpx.AsyncClient for these fetchers, with a scripted queue."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls = 0

    async def get(self, url, **kw):
        self.calls += 1
        nxt = self._queue.pop(0) if self._queue else _Resp(500)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_a_failed_fetch_returns_none_and_writes_nothing(conn):
    """This is the money assertion.

    `fetch_orders` used to swallow every failure and return `[]`. Cached, that
    turns one ESI hiccup into a page that says your orders are gone — and the
    next sync happily overwrites a good cache with the empty one. Returning
    None leaves the last good reading in place.
    """
    orders_api.save_cached_orders(conn, ALICE, [_order(1)])
    conn.commit()

    result = asyncio.run(
        orders_api.fetch_orders(_Client(_Resp(500)), ALICE, "tok", conn=conn))

    assert result is None, "a failed fetch reported an empty order book"
    kept, _ = orders_api.load_cached_orders(conn, ALICE)
    assert [o["order_id"] for o in kept] == [1], "the good cache was overwritten"


def test_a_successful_fetch_writes_the_cache(conn):
    result = asyncio.run(orders_api.fetch_orders(
        _Client(_Resp(200, [_order(7)])), ALICE, "tok", conn=conn))
    conn.commit()

    assert [o["order_id"] for o in result] == [7]
    cached, _ = orders_api.load_cached_orders(conn, ALICE)
    assert [o["order_id"] for o in cached] == [7]


def test_a_fetch_without_a_connection_does_not_write(conn):
    """The page must be able to call these without a cache side effect — and
    more to the point, a caller that forgets the connection should get data,
    not a silent no-op cache."""
    result = asyncio.run(
        orders_api.fetch_orders(_Client(_Resp(200, [_order(7)])), ALICE, "tok"))

    assert [o["order_id"] for o in result] == [7]
    assert orders_api.load_cached_orders(conn, ALICE)[0] is None


def test_history_failing_on_the_first_page_is_not_an_empty_history():
    """Same class of bug one level down, in `_get_all`."""
    out = asyncio.run(orders_api._get_all(_Client(_Resp(500)), "http://x", "tok"))

    assert out is None


def test_history_failing_on_a_later_page_keeps_what_arrived():
    """A partial history is worth more than none. Discarding four good pages
    because the fifth timed out is the other way to get this wrong."""
    client = _Client(_Resp(200, [_order(1)], pages=3),
                     _Resp(200, [_order(2)], pages=3),
                     _Resp(503))

    out = asyncio.run(orders_api._get_all(client, "http://x", "tok"))

    assert [o["order_id"] for o in out] == [1, 2]


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_router_cannot_open_an_esi_client_at_all():
    """Stronger than spying: `characters.py` no longer imports `esi_client`.

    It did until /orders and /wallet were converted — both handlers opened one,
    and both also asked ESI for the character's corporation id on every load.
    With the import gone there is no path from this module to a raw client, so
    the guard is a fact about the file rather than a fact about one request.

    If the import comes back, this fails and somebody has to say why. That is
    the point: the earlier version of this test monkeypatched
    `app.esi.client.esi_client`, which the router had already bound at import,
    so it guarded nothing and said so in a green tick.
    """
    from app.web.routers import characters as router_module

    assert not hasattr(router_module, "esi_client"), (
        "characters.py imports esi_client again — /orders and /wallet are "
        "supposed to render from cache. If a new handler here legitimately "
        "needs one, exempt it in tests/test_cache_only_routes.py::ALLOWED with "
        "a reason rather than deleting this.")


def test_the_contracts_router_still_needs_a_client_for_one_reason():
    """`contracts.py` keeps its `esi_client` import, unlike `characters.py`,
    and that is deliberate rather than an oversight.

    Two handlers there legitimately fetch: the public-contract region index,
    which is a streamed button, and `api_contract_items`, which is the user
    expanding one row. Both are exempted by name in
    `tests/test_cache_only_routes.py::ALLOWED`.

    Asserted so the difference between the two routers is a recorded decision.
    If contracts.py ever stops needing a client, this fails and the import
    should go the way characters.py's did.
    """
    from app.web.routers import contracts as router_module

    assert hasattr(router_module, "esi_client")


def test_no_converted_page_calls_a_fetcher(client, monkeypatch):
    """The import check above cannot see a fetch made through the API modules,
    which the routers *do* still import — they read the cache helpers next to
    the fetchers. So every `fetch_` on all three is replaced with a recorder.

    **The URL list is the test.** An earlier version patched the fetchers but
    visited only /orders and /wallet, so putting a fetch back into /contracts
    failed the AST scan and left this green. Every page named in the docstring
    has to appear in the loop below, or it is not covered.

    **It records rather than raises.** These handlers wrap their bodies in
    `except Exception` to turn a failure into an error banner, so a stub that
    raised was caught, rendered, and returned 200 — green test, page fetching
    on every request. Mutation is what surfaced that. A list survives being
    caught.
    """
    from app.character import contracts as contracts_api
    from app.character import orders as orders_api
    from app.character import wallet as wallet_api

    called: list[str] = []

    def _recorder(name):
        async def _spy(*a, **kw):
            called.append(name)
            return None
        return _spy

    patched = 0
    for module in (orders_api, wallet_api, contracts_api):
        for attr in dir(module):
            if attr.startswith("fetch_"):
                monkeypatch.setattr(module, attr, _recorder(f"{module.__name__}.{attr}"))
                patched += 1
    assert patched >= 12, f"only {patched} fetchers found — the scan has drifted"

    for url in ("/orders", "/orders?state=history", "/orders?scope=corp",
                "/orders?char=all", "/orders?char=all&scope=corp",
                "/wallet", "/wallet?scope=corp", "/wallet?scope=corp&division=3",
                "/contracts", "/contracts?scope=corp",
                "/contracts?char=all", "/contracts?char=all&scope=corp"):
        assert client.get(url).status_code == 200, url
        assert not called, f"{url} called {called}"


def test_an_unsynced_character_is_told_so_rather_than_shown_zero(client):
    """The page-level version of the None/[] distinction. The fixture's
    character has never been synced, so the cache is empty."""
    r = client.get("/orders")

    assert r.status_code == 200
    assert "Not synced yet" in r.text, (
        "the page showed an empty order book for a character it never looked at")


# ── /wallet, same shape ──────────────────────────────────────────────────────

from app.character import wallet as wallet_api  # noqa: E402  (grouped with its tests)


def test_an_unsynced_wallet_reads_as_none_not_an_empty_journal(conn):
    """The same distinction as orders, and it matters more here: an empty
    journal reads as "no activity this month", which is a finding rather than
    a gap."""
    rows, at = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL)

    assert rows is None
    assert at == 0.0


def test_a_wallet_ledger_round_trips(conn):
    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.JOURNAL,
                                  [{"id": 1, "amount": -5.0}])
    conn.commit()

    rows, at = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL)

    assert [r["id"] for r in rows] == [1]
    assert at > 0


def test_the_journal_and_transactions_do_not_overwrite_each_other(conn):
    """`ledger` is in the key. The worker writes both back to back, so a key
    that collapsed would leave whichever ran last in both tabs."""
    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.JOURNAL, [{"id": 1}])
    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.TRANSACTIONS, [{"id": 2}])
    conn.commit()

    journal, _ = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL)
    txns, _ = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.TRANSACTIONS)

    assert [r["id"] for r in journal] == [1]
    assert [r["id"] for r in txns] == [2]


def test_corporation_divisions_are_kept_apart(conn):
    """Seven divisions, shown one at a time. Without `division` in the key the
    page would show division 1's ledger under every tab — and the numbers look
    plausible, which is the worst kind of wrong for a wallet."""
    for div in (1, 3):
        wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL,
                                      [{"id": div}], wallet_api.CORPORATION, div)
    conn.commit()

    one, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 1)
    three, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 3)

    assert [r["id"] for r in one] == [1]
    assert [r["id"] for r in three] == [3]


def test_a_character_and_a_corporation_do_not_share_division_zero(conn):
    """A character sits at division 0 and a corporation's balance list does
    too. `owner_kind` is what keeps them apart."""
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": 1}])
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": 2}],
                                  wallet_api.CORPORATION)
    conn.commit()

    personal, _ = wallet_api.load_cached_ledger(conn, CORP, wallet_api.JOURNAL)
    corp, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION)

    assert [r["id"] for r in personal] == [1]
    assert [r["id"] for r in corp] == [2]


def test_a_failed_journal_fetch_keeps_the_previous_month(conn):
    """`fetch_journal` returned `[]` on any failure. Cached, that erases a
    month of history and the next sync writes the erasure down."""
    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.JOURNAL, [{"id": 1}])
    conn.commit()

    result = asyncio.run(
        wallet_api.fetch_journal(_Client(_Resp(500)), ALICE, "tok", conn=conn))

    assert result is None, "a failed fetch reported an empty journal"
    kept, _ = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL)
    assert [r["id"] for r in kept] == [1], "the good cache was overwritten"


def test_a_failed_transactions_fetch_writes_nothing(conn):
    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.TRANSACTIONS, [{"id": 1}])
    conn.commit()

    result = asyncio.run(
        wallet_api.fetch_transactions(_Client(_Resp(500)), ALICE, "tok", conn=conn))

    assert result is None
    kept, _ = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.TRANSACTIONS)
    assert [r["id"] for r in kept] == [1]


def test_the_balance_lands_in_the_table_the_dashboard_reads(conn):
    """Not the ledger table. `char_wallet_cache` has a second consumer in
    `app/web/main.py` that reads it with a five-minute TTL and fetches on a
    miss — writing it here is what stops that fetch happening at all."""
    asyncio.run(wallet_api.fetch_balance(
        _Client(_Resp(200, 12_345.75)), ALICE, "tok", conn=conn))
    conn.commit()

    stored = conn.execute(
        "SELECT balance FROM char_wallet_cache WHERE character_id=?",
        (ALICE,)).fetchone()

    assert stored is not None, "the balance did not reach char_wallet_cache"
    assert stored[0] == pytest.approx(12_345.75)
    assert wallet_api.load_cached_balance(conn, ALICE)[0] == pytest.approx(12_345.75)


def test_an_unsynced_wallet_page_says_so_rather_than_showing_zero(client):
    """The fixture's character has never been synced."""
    r = client.get("/wallet")

    assert r.status_code == 200
    assert "Not synced yet" in r.text, (
        "the page showed an empty wallet for a character it never looked at")


# ── /contracts, same shape again ─────────────────────────────────────────────

from app.character import contracts as contracts_api  # noqa: E402


def test_unsynced_contracts_read_as_none_not_an_empty_list(conn):
    """An expiring courier contract is exactly what this page is checked for.
    Showing none because nobody looked is the failure that costs collateral."""
    with _engine_conn(conn) as ec:
        rows, at = contracts_api.load_cached_contracts(ec, ALICE)

    assert rows is None
    assert at == 0.0


def test_contracts_round_trip_and_keep_owners_apart(conn):
    with _engine_conn(conn) as ec:
        contracts_api.save_cached_contracts(ec, ALICE, [{"contract_id": 1}])
        contracts_api.save_cached_contracts(ec, CORP, [{"contract_id": 2}],
                                            contracts_api.CORPORATION)
        ec.commit()

        mine, _ = contracts_api.load_cached_contracts(ec, ALICE)
        theirs, _ = contracts_api.load_cached_contracts(
            ec, CORP, contracts_api.CORPORATION)

    assert [c["contract_id"] for c in mine] == [1]
    assert [c["contract_id"] for c in theirs] == [2]


def test_a_character_and_a_corporation_with_one_id_stay_apart(conn):
    """`owner_kind` is in the key for the same reason as everywhere else."""
    with _engine_conn(conn) as ec:
        contracts_api.save_cached_contracts(ec, CORP, [{"contract_id": 1}])
        contracts_api.save_cached_contracts(ec, CORP, [{"contract_id": 2}],
                                            contracts_api.CORPORATION)
        ec.commit()

        personal, _ = contracts_api.load_cached_contracts(ec, CORP)
        corp, _ = contracts_api.load_cached_contracts(
            ec, CORP, contracts_api.CORPORATION)

    assert [c["contract_id"] for c in personal] == [1]
    assert [c["contract_id"] for c in corp] == [2]


def test_a_failed_contracts_fetch_keeps_the_previous_list(conn):
    with _engine_conn(conn) as ec:
        contracts_api.save_cached_contracts(ec, ALICE, [{"contract_id": 1}])
        ec.commit()

        result = asyncio.run(contracts_api.fetch_character_contracts(
            _Client(_Resp(500)), ALICE, "tok", conn=ec))

        assert result is None, "a failed fetch reported an empty contract list"
        kept, _ = contracts_api.load_cached_contracts(ec, ALICE)
    assert [c["contract_id"] for c in kept] == [1]


def test_contract_items_are_cached_without_an_age(conn):
    """The one cache here that cannot go stale: a contract's contents are fixed
    when it is created. So this returns the items alone, with no timestamp to
    judge — offering one would invite a staleness check that means nothing."""
    with _engine_conn(conn) as ec:
        assert contracts_api.load_cached_contract_items(ec, 555) is None

        contracts_api.save_cached_contract_items(ec, 555, [{"type_id": 34}])
        ec.commit()

        items = contracts_api.load_cached_contract_items(ec, 555)
    assert [i["type_id"] for i in items] == [34]


def test_a_failed_item_fetch_is_not_cached_as_an_empty_contract(conn):
    """Caching `[]` here would be permanent — nothing ever refreshes it — so a
    single failed expand would show that contract as empty forever."""
    with _engine_conn(conn) as ec:
        result = asyncio.run(contracts_api.fetch_character_contract_items(
            _Client(_Resp(500)), ALICE, 777, "tok", conn=ec))
        ec.commit()

        assert result is None
        assert contracts_api.load_cached_contract_items(ec, 777) is None, (
            "a failed fetch was cached permanently as an empty contract")


def test_the_contracts_page_reads_the_cache(client, monkeypatch):
    """Covered by `test_neither_page_calls_a_fetcher` for the fetchers; this is
    the page-level statement of the None/[] distinction."""
    r = client.get("/contracts")

    assert r.status_code == 200
    assert "Not synced yet" in r.text


# ── /assets and /blueprints: a different starting point ──────────────────────

from app.character import assets as assets_api      # noqa: E402
from app.character import blueprints as bp_api      # noqa: E402


def test_the_asset_reader_ignores_the_ttl(conn):
    """These two caches already existed and the worker already filled them.
    What made the pages fetch was `CACHE_TTL`: `_load_cache` returns None once
    the row is older than ten minutes, and the page then went to ESI.

    The TTL answers "is another round trip worth it", which is a question for
    the fetcher. A page that must not make round trips has no use for it.
    """
    stale = time.time() - assets_api.CACHE_TTL - 3600      # an hour past expiry
    conn.execute(
        "INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
        " VALUES (?,?,?)",
        (ALICE, json.dumps([{"item_id": 1, "type_id": 34, "location_id": 60003760,
                             "quantity": 5, "is_singleton": False,
                             "location_flag": "Hangar"}]), stale))
    conn.commit()

    with _engine_conn(conn) as ec:
        assert assets_api._load_cache(ec, ALICE) is None, (
            "the fixture is not actually stale — this test would pass vacuously")

        assets, at = assets_api.load_cached_assets(ec, ALICE)

    assert assets is not None, "an aged cache read as never-synced"
    assert len(assets) == 1
    assert at == pytest.approx(stale)


def test_the_blueprint_reader_ignores_the_ttl(conn):
    stale = time.time() - bp_api.CACHE_TTL - 3600
    conn.execute(
        "INSERT INTO char_blueprints_cache (character_id, data_json, cached_at)"
        " VALUES (?,?,?)",
        (ALICE, json.dumps([{"item_id": 9, "type_id": 999, "location_id": 60003760,
                             "quantity": -1, "material_efficiency": 10,
                             "time_efficiency": 20, "runs": -1,
                             "location_flag": "Hangar"}]), stale))
    conn.commit()

    with _engine_conn(conn) as ec:
        assert bp_api._load_cache(ec, ALICE) is None
        bps, at = bp_api.load_cached_blueprints(ec, ALICE)

    assert bps is not None and len(bps) == 1
    assert at == pytest.approx(stale)


def test_an_unsynced_character_has_no_assets_rather_than_none(conn):
    """Same distinction as everywhere else. An empty hangar is a statement."""
    with _engine_conn(conn) as ec:
        assert assets_api.load_cached_assets(ec, ALICE) == (None, 0.0)
        assert bp_api.load_cached_blueprints(ec, ALICE) == (None, 0.0)


def test_container_ids_are_derived_from_containment(conn):
    """No category list to keep in step with CCP: an item is a container
    exactly when something else is located inside it."""
    assets = [
        {"item_id": 1, "location_id": 60003760},   # a can in a station
        {"item_id": 2, "location_id": 1},          # something inside the can
        {"item_id": 3, "location_id": 60003760},   # a ship holding nothing
    ]

    assert assets_api.container_item_ids(assets) == [1]


def test_container_ids_accept_objects_as_well_as_dicts(conn):
    """Both shapes circulate — `_parse_assets` makes objects, `_load_cache`
    returns raw dicts — and this was found by an AttributeError from inside a
    sync tick rather than by reading."""
    parsed = assets_api._parse_assets([
        {"item_id": 1, "type_id": 34, "location_id": 60003760, "quantity": 1,
         "is_singleton": True, "location_flag": "Hangar"},
        {"item_id": 2, "type_id": 34, "location_id": 1, "quantity": 1,
         "is_singleton": False, "location_flag": "Cargo"},
    ])

    assert assets_api.container_item_ids(parsed) == [1]


def test_container_names_round_trip(conn):
    with _engine_conn(conn) as ec:
        assets_api.save_cached_container_names(ec, {111: "Ammo Bin", 222: "Ore"})
        ec.commit()

        assert assets_api.load_cached_container_names(
            ec, [111, 333]) == {111: "Ammo Bin"}


def test_a_failed_name_fetch_caches_nothing(conn):
    """A name that failed to resolve must not be stored as a name, and must not
    wipe one that resolved earlier."""
    class _Failing:
        async def post(self, *a, **k):
            raise RuntimeError("ESI down")

    with _engine_conn(conn) as ec:
        assets_api.save_cached_container_names(ec, {111: "Ammo Bin"})
        ec.commit()

        got = asyncio.run(assets_api.fetch_container_names(
            _Failing(), 1, "tok", [111], conn=ec))

        assert got is None
        assert assets_api.load_cached_container_names(
            ec, [111]) == {111: "Ammo Bin"}


def test_neither_inventory_page_calls_a_fetcher(client, monkeypatch):
    """The same guard as the other three pages, pointed at these two."""
    called: list[str] = []

    def _recorder(name):
        async def _spy(*a, **kw):
            called.append(name)
            return None
        return _spy

    patched = 0
    for module in (assets_api, bp_api):
        for attr in dir(module):
            if attr.startswith("fetch_"):
                monkeypatch.setattr(module, attr, _recorder(f"{module.__name__}.{attr}"))
                patched += 1
    assert patched >= 4, f"only {patched} fetchers found"

    for url in ("/assets", "/assets?view=all", "/blueprints", "/blueprints?view=all"):
        assert client.get(url).status_code == 200, url
        assert not called, f"{url} called {called}"


# ── /planets, /pi-planner and the alert tile ─────────────────────────────────

from app.character import planets as planets_api    # noqa: E402


def test_an_unsynced_character_has_no_colonies_rather_than_none(conn):
    """PI is the one thing in this app you check *because* you expect to have
    forgotten about it. Showing no colonies for a character nobody looked at is
    the most misleading version of this failure in the whole conversion."""
    assert planets_api.load_cached_colonies(conn, ALICE) == (None, 0.0)


def test_a_character_with_no_colonies_is_distinguishable_from_an_unsynced_one(conn):
    planets_api.save_cached_colonies(conn, ALICE, [], [])
    conn.commit()

    result, at = planets_api.load_cached_colonies(conn, ALICE)

    assert result == ([], []), "synced-and-empty was reported as never-synced"
    assert at > 0


def test_forbidden_is_cached_as_itself(conn):
    """A token predating the PI scope is a durable fact, not a transient
    failure: re-discovering it every tick costs a call per character forever,
    and the UI response is a re-auth prompt rather than a retry."""
    planets_api.save_cached_colonies(conn, ALICE, [], [], planets_api.FORBIDDEN)
    conn.commit()

    result, _at = planets_api.load_cached_colonies(conn, ALICE)

    assert result == planets_api.FORBIDDEN


def test_colonies_and_details_stay_paired(conn):
    """`details` is aligned positionally with `colonies`. A detail call that
    failed leaves None in its slot rather than being dropped — dropping it
    would silently re-pair every colony after it with another planet's pins."""
    colonies = [{"planet_id": 1}, {"planet_id": 2}, {"planet_id": 3}]
    details = [{"pins": ["a"]}, None, {"pins": ["c"]}]
    planets_api.save_cached_colonies(conn, ALICE, colonies, details)
    conn.commit()

    (got_colonies, got_details), _at = planets_api.load_cached_colonies(conn, ALICE)

    assert [c["planet_id"] for c in got_colonies] == [1, 2, 3]
    assert got_details[1] is None, "the failed slot was dropped, shifting the pairing"
    assert got_details[2] == {"pins": ["c"]}


def test_a_failed_colony_list_leaves_the_previous_one(conn):
    """None from `fetch_planets` is transient — ESI being unavailable must not
    erase colonies that are still there and still running."""
    planets_api.save_cached_colonies(conn, ALICE, [{"planet_id": 1}], [{"pins": []}])
    conn.commit()

    class _NoColonies:
        async def get(self, *a, **k):
            raise RuntimeError("ESI down")

    result = asyncio.run(planets_api.fetch_colonies(_NoColonies(), ALICE, "tok", conn=conn))

    assert result is None
    kept, _at = planets_api.load_cached_colonies(conn, ALICE)
    assert kept[0] == [{"planet_id": 1}], "a transient failure erased the colonies"


def test_planet_names_are_read_without_fetching(conn):
    """They never change, so the worker resolves each one once, ever. The page
    reads whatever is known and falls back to the id for the rest."""
    conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                 (4001, "Testworld IV"))
    conn.commit()

    assert planets_api.load_planet_names(conn, [4001, 4002]) == {4001: "Testworld IV"}


def test_the_pi_pages_do_not_call_a_fetcher(client, monkeypatch):
    """/planets and /pi-planner between them were the most call-hungry pages in
    the app: one colony-list call per character plus one detail call per
    planet, on every view."""
    called: list[str] = []

    def _recorder(name):
        async def _spy(*a, **kw):
            called.append(name)
            return None
        return _spy

    patched = 0
    for attr in dir(planets_api):
        if attr.startswith("fetch_"):
            monkeypatch.setattr(planets_api, attr, _recorder(attr))
            patched += 1
    assert patched >= 3, f"only {patched} fetchers found"

    for url in ("/planets", "/pi-planner", "/api/pi-alert-count", "/api/dashboard/pi-alerts", "/api/dashboard/pi-alerts?force=1"):
        assert client.get(url).status_code == 200, url
        assert not called, f"{url} called {called}"


def test_forcing_the_alert_tile_wakes_the_worker_instead_of_fetching(client):
    """`force=1` used to mean "go to ESI now" — up to eighty round trips from a
    dashboard tile. It asks the worker to, and answers immediately from what is
    known."""
    r = client.get("/api/dashboard/pi-alerts?force=1")

    assert r.status_code == 200
    assert "refresh_requested" in r.json(), (
        "force no longer reports whether the worker was woken")


class _PiClient:
    """Enough of httpx for `fetch_colonies`: routes by URL shape.

    `list_status` drives the colony-list call; `detail_ok` decides whether the
    per-planet call succeeds. Both are needed because the two failures mean
    different things and the fetcher treats them differently.
    """

    def __init__(self, list_status=200, colonies=None, detail_ok=True):
        self.list_status, self.colonies, self.detail_ok = list_status, colonies or [], detail_ok
        self.calls: list[str] = []

    async def get(self, url, **kw):
        self.calls.append(url)
        if url.endswith("/planets/"):
            return _Resp(self.list_status, self.colonies)
        if not self.detail_ok:
            raise RuntimeError("detail call failed")
        return _Resp(200, {"pins": [{"pin_id": 1}], "links": [], "routes": []})


def test_a_forbidden_token_is_cached_by_the_fetcher(conn):
    """Not just storable — actually stored. A 403 means the token predates the
    PI scope, which is durable: re-discovering it costs one call per character
    on every tick, forever, and the answer never changes until a re-auth."""
    client = _PiClient(list_status=403)

    result = asyncio.run(planets_api.fetch_colonies(client, ALICE, "tok", conn=conn))
    conn.commit()

    assert result == planets_api.FORBIDDEN
    assert planets_api.load_cached_colonies(conn, ALICE)[0] == planets_api.FORBIDDEN, (
        "the 403 was returned but not written down, so the next tick asks again")


def test_the_fetcher_keeps_a_failed_detail_in_its_slot(conn):
    """`details` is aligned positionally with `colonies`. Dropping a failed one
    shifts every colony after it onto another planet's pins — a page that looks
    entirely plausible and attributes your extractors to the wrong worlds."""
    client = _PiClient(colonies=[{"planet_id": 1}, {"planet_id": 2}], detail_ok=False)

    result = asyncio.run(planets_api.fetch_colonies(client, ALICE, "tok", conn=conn))
    conn.commit()

    colonies, details = result
    assert len(details) == len(colonies) == 2, (
        f"{len(details)} details for {len(colonies)} colonies — the pairing shifted")
    assert details == [None, None]

    (_c, cached_details), _at = planets_api.load_cached_colonies(conn, ALICE)
    assert cached_details == [None, None]


def test_a_character_with_no_colonies_is_written_down_as_such(conn):
    """An empty colony list is conclusive and worth caching: otherwise every
    tick re-asks, and the page cannot tell "no PI" from "not looked at"."""
    client = _PiClient(colonies=[])

    result = asyncio.run(planets_api.fetch_colonies(client, ALICE, "tok", conn=conn))
    conn.commit()

    assert result == ([], [])
    assert planets_api.load_cached_colonies(conn, ALICE)[0] == ([], [])


# ── /plan, the last pair ─────────────────────────────────────────────────────

def test_the_plan_pages_do_not_call_a_collection_fetcher(client, monkeypatch):
    """`plan_result` submits repeatedly while somebody tunes ME, runs and
    stations, and each submission used to re-fetch the same three paginated
    lists to compute a different number from identical inputs.

    Not the AST scan's job: it matches on the *name* `fetch_blueprints`, so a
    local import aliased to something else walks straight past it. This records
    calls instead, which an alias cannot dodge.

    The product-name resolve is deliberately outside this: `plan_result` keeps
    its exemption for that one call, and it is skipped entirely for any product
    the SDE knows — which is every product with a blueprint.
    """
    from app.character import assets as a_api
    from app.character import blueprints as b_api
    from app.character import skills as s_api

    called: list[str] = []

    def _recorder(name):
        async def _spy(*a, **kw):
            called.append(name)
            return None
        return _spy

    patched = 0
    for module in (a_api, b_api, s_api):
        for attr in dir(module):
            if attr.startswith("fetch_"):
                monkeypatch.setattr(module, attr, _recorder(f"{module.__name__}.{attr}"))
                patched += 1
    assert patched >= 4, f"only {patched} fetchers found"

    assert client.get("/plan").status_code == 200
    assert not called, f"/plan called {called}"

    client.post("/plan", data={"product": "Tritanium", "qty": "1",
                               "station": "60003760", "mode": "full",
                               "runs_per_job": "0", "form_me": "0"})
    assert not called, f"POST /plan called {called}"


def test_planning_for_an_unsynced_character_says_so(client, monkeypatch):
    """The cache reads return None for a character the worker has not reached.
    Treating that as "owns nothing" would price the whole build as if every
    component had to be bought — a plausible number, and wrong in the expensive
    direction."""
    # Patched on the *router*, not on `app.character.assets`: plan.py does
    # `from app.character.assets import load_cached_assets` at module level,
    # which binds the function, so rebinding the source leaves the router's copy
    # alone. Third time this has come up in this conversion; it is in the
    # worklist under "Two ways a test can guard nothing".
    from app.web.routers import plan as plan_router

    monkeypatch.setattr(plan_router, "load_cached_assets", lambda conn, cid: (None, 0.0))

    r = client.post("/plan", data={"product": "Tritanium", "qty": "1",
                                   "station": "60003760", "mode": "full",
                                   "runs_per_job": "0", "form_me": "0"})

    assert r.status_code == 200
    assert "has not been synced yet" in r.text, (
        "an unsynced character was planned as owning nothing")
