"""`app/character/orders.py`, before it moves onto the portable query layer.

`tests/test_orders_cache.py` already covers most of this module well: the
three-part key `(owner_id, owner_kind, state)` separating owners, kinds and
active-from-history; a corrupt row reading as unsynced; a second sync replacing
rather than appending; and `fetch_orders` writing, not writing on failure, and
tolerating no connection. `_get_all`'s first-page-versus-later-page rule is
covered too. All of that stays where it is.

**The two corporation fetchers have no test.** `fetch_corp_orders` and
`fetch_corp_orders_history` are the only callers that write with
`kind="corporation"`, so half the key's `owner_kind` axis is exercised on the
write path by nothing at all — the existing key tests call `save_cached_orders`
directly. `fetch_corp_orders` also paginates itself rather than using
`_get_all`, because it needs the first response's status code to tell a missing
role from an outage, and that hand-rolled loop is untested.

**These assertions are written against the `sqlite3` version on purpose**, so
the conversion is judged by whether it preserves them.

Two things here are conversion traps rather than ordinary behaviour:

* **The writer does not commit.** The caller owns the transaction boundary.
* **`fetch_corp_orders` answers with a reason, not just a value.** A 403 means
  the character lacks the in-game role, which is the one failure the user can
  actually fix; reporting it as a generic HTTP error sends them looking for an
  outage instead.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.character import orders as orders_api
from app.db.schema import apply_schema

ALICE = 2_112_625_428
CORP = 98_000_001


# ── stubs ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, payload=None, pages: int = 1):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = {"x-pages": str(pages)}

    def json(self):
        return self._payload


class _Client:
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


def _order(order_id, type_id=34):
    return {"order_id": order_id, "type_id": type_id, "price": 5.0}


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "orders.db"))
    apply_schema(c)
    yield c
    c.close()


def _rows(conn, owner_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM market_orders_cache WHERE owner_id=?",
        (owner_id,)).fetchone()[0]


# ── the writer's transaction boundary ────────────────────────────────────────

def test_the_second_save_moves_the_age_forward(conn):
    """`cached_at=excluded.cached_at` in the DO UPDATE. Without it the row keeps
    its first timestamp, and the page reports a book it just refreshed as
    stale — which on /orders is the difference between "your sell order is
    still up" and "this reading is an hour old, go and look"."""
    import time as _time

    orders_api.save_cached_orders(conn, ALICE, [_order(1)])
    conn.commit()
    _, first = orders_api.load_cached_orders(conn, ALICE)

    conn.execute(
        "UPDATE market_orders_cache SET cached_at=? WHERE owner_id=?",
        (first - 3600, ALICE))
    conn.commit()
    assert orders_api.load_cached_orders(conn, ALICE)[1] == pytest.approx(first - 3600), (
        "the backdate did not take — this test would pass vacuously")

    _time.sleep(0.01)
    orders_api.save_cached_orders(conn, ALICE, [_order(2)])
    conn.commit()

    _, second = orders_api.load_cached_orders(conn, ALICE)
    assert second > first - 3600, "the upsert kept the old timestamp"


def test_the_writer_does_not_commit(conn, tmp_path):
    """Both halves matter — only the first would also pass if the writer never
    wrote anything at all."""
    orders_api.save_cached_orders(conn, ALICE, [_order(1)])

    other = sqlite3.connect(str(tmp_path / "orders.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM market_orders_cache WHERE owner_id=?",
            (ALICE,)).fetchone()[0] == 0, (
            "the writer committed — the caller's boundary moved into it")
    finally:
        other.close()

    conn.commit()
    other = sqlite3.connect(str(tmp_path / "orders.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM market_orders_cache WHERE owner_id=?",
            (ALICE,)).fetchone()[0] == 1, "the caller's commit did not land"
    finally:
        other.close()


# ── fetch_corp_orders: answers with a reason ─────────────────────────────────

def test_corp_orders_are_cached_as_corporation_active(conn):
    """The only writer of `kind="corporation", state="active"`. Filed under the
    character kind, a corporation's order book would appear as the pilot's own
    — and the numbers look entirely plausible."""
    client = _Client(_Resp(200, [_order(1)]))

    got, err = asyncio.run(orders_api.fetch_corp_orders(client, CORP, "tok", conn=conn))
    conn.commit()

    assert err is None
    assert [o["order_id"] for o in got] == [1]
    cached, _ = orders_api.load_cached_orders(
        conn, CORP, orders_api.CORPORATION, orders_api.ACTIVE)
    assert [o["order_id"] for o in cached] == [1]
    assert orders_api.load_cached_orders(
        conn, CORP, orders_api.CHARACTER, orders_api.ACTIVE) == (None, 0.0)


def test_corp_orders_do_not_overwrite_the_corporations_history(conn):
    """`state` is the third part of the key, and the worker writes both."""
    orders_api.save_cached_orders(conn, CORP, [_order(9)],
                                  orders_api.CORPORATION, orders_api.HISTORY)
    conn.commit()

    asyncio.run(orders_api.fetch_corp_orders(
        _Client(_Resp(200, [_order(1)])), CORP, "tok", conn=conn))
    conn.commit()

    history, _ = orders_api.load_cached_orders(
        conn, CORP, orders_api.CORPORATION, orders_api.HISTORY)
    assert [o["order_id"] for o in history] == [9], "active overwrote history"


def test_corp_orders_403_names_the_missing_role(conn):
    """The one failure the user can fix. A generic HTTP error would send them
    looking for an outage instead of at their corporation roles."""
    got, err = asyncio.run(orders_api.fetch_corp_orders(
        _Client(_Resp(403)), CORP, "tok", conn=conn))

    assert got is None
    assert "Trader" in err or "Accountant" in err


def test_corp_orders_other_errors_carry_their_code(conn):
    got, err = asyncio.run(orders_api.fetch_corp_orders(
        _Client(_Resp(503)), CORP, "tok", conn=conn))

    assert got is None
    assert "503" in err


def test_corp_orders_transport_failure_is_reported_not_swallowed(conn):
    got, err = asyncio.run(orders_api.fetch_corp_orders(
        _Client(RuntimeError("connection reset")), CORP, "tok", conn=conn))

    assert got is None
    assert "connection reset" in err


def test_a_failed_corp_orders_fetch_keeps_the_previous_book(conn):
    orders_api.save_cached_orders(conn, CORP, [_order(1)],
                                  orders_api.CORPORATION, orders_api.ACTIVE)
    conn.commit()

    asyncio.run(orders_api.fetch_corp_orders(
        _Client(_Resp(403)), CORP, "tok", conn=conn))
    conn.commit()

    kept, _ = orders_api.load_cached_orders(
        conn, CORP, orders_api.CORPORATION, orders_api.ACTIVE)
    assert [o["order_id"] for o in kept] == [1]


def test_corp_orders_walks_its_own_pages(conn):
    """It does not use `_get_all` — it needs the first response's status code
    to tell a missing role from an outage, so it paginates by hand."""
    client = _Client(_Resp(200, [_order(1)], pages=3),
                     _Resp(200, [_order(2)], pages=3),
                     _Resp(200, [_order(3)], pages=3))

    got, err = asyncio.run(orders_api.fetch_corp_orders(client, CORP, "tok", conn=conn))

    assert err is None
    assert [o["order_id"] for o in got] == [1, 2, 3]
    assert [p.get("page") for p in client.params] == [1, 2, 3]


def test_a_later_corp_orders_page_failing_keeps_what_arrived(conn):
    """A partial book beats none, once we know the corporation has orders."""
    client = _Client(_Resp(200, [_order(1)], pages=3), _Resp(500))

    got, err = asyncio.run(orders_api.fetch_corp_orders(client, CORP, "tok", conn=conn))
    conn.commit()

    assert err is None
    assert [o["order_id"] for o in got] == [1]
    cached, _ = orders_api.load_cached_orders(
        conn, CORP, orders_api.CORPORATION, orders_api.ACTIVE)
    assert [o["order_id"] for o in cached] == [1]


def test_corp_orders_sends_the_token(conn):
    client = _Client(_Resp(200, []))

    asyncio.run(orders_api.fetch_corp_orders(client, CORP, "tok-abc", conn=conn))

    assert client.headers[0].get("Authorization") == "Bearer tok-abc"


def test_corp_orders_without_a_connection_still_returns(conn):
    got, err = asyncio.run(orders_api.fetch_corp_orders(
        _Client(_Resp(200, [_order(1)])), CORP, "tok"))

    assert err is None
    assert [o["order_id"] for o in got] == [1]
    assert orders_api.load_cached_orders(
        conn, CORP, orders_api.CORPORATION, orders_api.ACTIVE) == (None, 0.0)


# ── _get_all: a transport failure is not an empty history ────────────────────

def test_a_first_page_transport_failure_is_not_an_empty_history(conn):
    """`test_history_failing_on_the_first_page_is_not_an_empty_history` in
    `tests/test_orders_cache.py` covers the *status code* branch. This is the
    `except Exception` one, and it was not covered: a mutation that dropped the
    `page == 1` check there failed nothing, so a connection reset on the first
    page would have been written down as "this character has no order history"
    and cached.
    """
    orders_api.save_cached_orders(conn, ALICE, [_order(1)],
                                  orders_api.CHARACTER, orders_api.HISTORY)
    conn.commit()

    got = asyncio.run(orders_api.fetch_orders_history(
        _Client(RuntimeError("connection reset")), ALICE, "tok", conn=conn))
    conn.commit()

    assert got is None, "a transport failure reported as an empty history"
    kept, _ = orders_api.load_cached_orders(
        conn, ALICE, orders_api.CHARACTER, orders_api.HISTORY)
    assert [o["order_id"] for o in kept] == [1]


def test_a_later_page_transport_failure_keeps_what_arrived(conn):
    """The other side of the same branch: once page one has succeeded, a
    partial history beats none."""
    client = _Client(_Resp(200, [_order(1)], pages=3),
                     RuntimeError("connection reset"))

    got = asyncio.run(orders_api.fetch_orders_history(client, ALICE, "tok", conn=conn))

    assert [o["order_id"] for o in got] == [1]


# ── the two history fetchers ─────────────────────────────────────────────────

def test_character_history_is_cached_under_the_history_state(conn):
    client = _Client(_Resp(200, [_order(1)]))

    got = asyncio.run(orders_api.fetch_orders_history(client, ALICE, "tok", conn=conn))
    conn.commit()

    assert [o["order_id"] for o in got] == [1]
    cached, _ = orders_api.load_cached_orders(
        conn, ALICE, orders_api.CHARACTER, orders_api.HISTORY)
    assert [o["order_id"] for o in cached] == [1]
    assert orders_api.load_cached_orders(
        conn, ALICE, orders_api.CHARACTER, orders_api.ACTIVE) == (None, 0.0), (
        "history was written into the active slot")


def test_corp_history_is_cached_under_corporation_history(conn):
    """The fourth corner of the key, and the only writer of it."""
    client = _Client(_Resp(200, [_order(1)]))

    got = asyncio.run(orders_api.fetch_corp_orders_history(
        client, CORP, "tok", conn=conn))
    conn.commit()

    assert [o["order_id"] for o in got] == [1]
    cached, _ = orders_api.load_cached_orders(
        conn, CORP, orders_api.CORPORATION, orders_api.HISTORY)
    assert [o["order_id"] for o in cached] == [1]
    assert orders_api.load_cached_orders(
        conn, CORP, orders_api.CHARACTER, orders_api.HISTORY) == (None, 0.0)


@pytest.mark.parametrize("fetcher, owner", [
    ("fetch_orders_history", ALICE),
    ("fetch_corp_orders_history", CORP),
])
def test_a_failed_history_fetch_writes_nothing(conn, fetcher, owner):
    """`orders is None` guards the save. Without it a transport blip replaces a
    real history with an empty one, permanently as far as the page is
    concerned."""
    kind = (orders_api.CHARACTER if fetcher == "fetch_orders_history"
            else orders_api.CORPORATION)
    orders_api.save_cached_orders(conn, owner, [_order(1)], kind, orders_api.HISTORY)
    conn.commit()

    got = asyncio.run(getattr(orders_api, fetcher)(
        _Client(_Resp(500)), owner, "tok", conn=conn))
    conn.commit()

    assert got is None
    kept, _ = orders_api.load_cached_orders(conn, owner, kind, orders_api.HISTORY)
    assert [o["order_id"] for o in kept] == [1]


@pytest.mark.parametrize("fetcher, owner", [
    ("fetch_orders_history", ALICE),
    ("fetch_corp_orders_history", CORP),
])
def test_an_empty_history_is_still_a_sync(conn, fetcher, owner):
    """`[]` from ESI means "no closed orders", which is a real answer and must
    be written down — otherwise the page offers to sync forever."""
    got = asyncio.run(getattr(orders_api, fetcher)(
        _Client(_Resp(200, [])), owner, "tok", conn=conn))
    conn.commit()

    assert got == []
    assert _rows(conn, owner) == 1
