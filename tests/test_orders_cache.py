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

import pytest

from app.character import orders as orders_api
from app.db.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "orders.db"))
    apply_schema(c)
    yield c
    c.close()


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


def test_neither_page_calls_a_fetcher(client, monkeypatch):
    """The import check above cannot see a fetch made through the API modules,
    which the router *does* still import — it reads their cache helpers. So
    every `fetch_` on both is replaced with a recorder.

    **It records rather than raises.** These handlers wrap their bodies in
    `except Exception` to turn a failure into an error banner, so a stub that
    raised was caught, rendered, and returned 200 — green test, page fetching
    on every request. Mutation is what surfaced that. A list survives being
    caught.
    """
    from app.character import orders as orders_api
    from app.character import wallet as wallet_api

    called: list[str] = []

    def _recorder(name):
        async def _spy(*a, **kw):
            called.append(name)
            return None
        return _spy

    patched = 0
    for module in (orders_api, wallet_api):
        for attr in dir(module):
            if attr.startswith("fetch_"):
                monkeypatch.setattr(module, attr, _recorder(f"{module.__name__}.{attr}"))
                patched += 1
    assert patched >= 8, f"only {patched} fetchers found — the scan has drifted"

    for url in ("/orders", "/orders?state=history", "/orders?scope=corp",
                "/orders?char=all", "/orders?char=all&scope=corp",
                "/wallet", "/wallet?scope=corp", "/wallet?scope=corp&division=3"):
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
