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

def test_the_orders_page_never_calls_esi(client, monkeypatch):
    """The scan in `test_cache_only_routes.py` proves the handler contains no
    `fetch_` call. It cannot prove the handler does not reach ESI some other
    way — through a helper in another module, or a bare client. This can.

    **Patched on the router, not on `app.esi.client`.** The router does
    `from app.esi.client import esi_client`, which binds the function at import;
    rebinding the source module afterwards leaves that binding untouched and
    this test green no matter what the page does. The first version of this
    test did exactly that, and mutation is what caught it — restoring an
    `esi_client()` call to the handler failed the AST scan and *not* this.

    `setattr` with the string form is deliberate too: it raises here if the
    router stops importing the name, rather than silently guarding nothing.

    **And it records rather than raises.** The handler wraps its body in
    `except Exception`, so a stub that raised `AssertionError` was caught,
    turned into an error banner, and returned 200 — the test passed while the
    page called ESI on every request. Mutation is what surfaced that: putting
    an `esi_client()` call back into the handler failed the AST scan and left
    this green. A list the stub appends to survives being swallowed.
    """
    from app.web.routers import characters as router_module

    calls: list[str] = []

    def _spy(*a, **kw):
        calls.append("esi_client")
        raise AssertionError("the orders page called ESI")

    assert hasattr(router_module, "esi_client"), (
        "the router no longer imports esi_client — retarget this test rather "
        "than deleting it")
    monkeypatch.setattr(router_module, "esi_client", _spy)

    for url in ("/orders", "/orders?state=history", "/orders?scope=corp",
                "/orders?char=all", "/orders?char=all&scope=corp"):
        assert client.get(url).status_code == 200, url
        assert not calls, f"{url} opened an ESI client"


def test_an_unsynced_character_is_told_so_rather_than_shown_zero(client):
    """The page-level version of the None/[] distinction. The fixture's
    character has never been synced, so the cache is empty."""
    r = client.get("/orders")

    assert r.status_code == 200
    assert "Not synced yet" in r.text, (
        "the page showed an empty order book for a character it never looked at")
