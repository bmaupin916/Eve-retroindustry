"""`app/character/wallet.py`, before it moves onto the portable query layer.

`tests/test_orders_cache.py` already covers the character paths — unsynced
reads as `None`, the ledger round-trips, `ledger` and `division` keep rows
apart, a failed journal or transactions fetch writes nothing, and the balance
lands in the table the dashboard reads. `tests/test_wallet_filter.py` covers
`humanize_ref_type` and the journal's paging bound. All of that stays where it
is.

**The three corporation fetchers have no test at all**, and they are the ones
that matter here: `wallet_ledger_cache` has a **four-part** primary key
`(owner_id, owner_kind, division, ledger)`, and the character paths exercise
only `kind="character", division=0`. Half the key is never varied by the
existing tests, so half the conflict target could be wrong without anything
noticing.

**These assertions are unchanged by the conversion.** They were written against
the `sqlite3` version first, so the rewrite could be judged by whether it
preserves them. Only the fixture underneath moved, and it now runs each of them
on both backends.

Four things here are conversion traps rather than ordinary behaviour:

* **A four-part conflict target.** `ON CONFLICT (owner_id, owner_kind,
  division, ledger)` is the widest in the codebase, and getting it wrong does
  not raise — it inserts a second row. The failure surfaces as a wallet tab
  showing another division's money, which looks entirely plausible.
* **Corporation *balances* break the division rule deliberately.** ESI returns
  every division's balance in one response, so they are stored once at
  `division=0` under `ledger="balances"` rather than split across seven rows
  that would all have to be written together to stay consistent. Division 0 is
  also the character's slot — the two never collide because `owner_kind`
  differs.
* **Neither writer commits.** The caller owns the transaction boundary.
* **A first page failing is not "no journal".** Both journal fetchers return
  `None` when page one fails and a partial list when a later page does, and the
  save is skipped entirely on the `None` path. Cached, an empty journal erases
  a month of history and the next sync writes the erasure down.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import create_engine, text

from app.character import wallet as wallet_api
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_character_wallet"

ALICE = 2_112_625_428
CORP = 98_000_001

#: A real-sized balance. ISK runs to trillions, and the point of using one here
#: is that it is past 2**32 and has cents — a column that quietly became single
#: precision would round it and still look like a number.
BIG_ISK = 1_234_567_890_123.45


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


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    """An engine per backend, built once for the module.

    Function scope here would drop and rebuild a Postgres schema — every
    migration — for each test in the file; clearing the two tables between
    tests is the same isolation for a fraction of the cost.
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
_CLEARED = ("wallet_ledger_cache", "char_wallet_cache")


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
def conn(engine):
    with engine.connect() as c:
        yield c


def _backend(conn) -> str:
    return conn.engine.dialect.name


def _ledger_rows(conn, owner_id: int) -> int:
    return conn.execute(
        text("SELECT COUNT(*) FROM wallet_ledger_cache WHERE owner_id=:owner_id"),
        {"owner_id": owner_id}).fetchone()[0]


def _balance_rows(conn, char_id: int) -> int:
    return conn.execute(
        text("SELECT COUNT(*) FROM char_wallet_cache WHERE character_id=:cid"),
        {"cid": char_id}).fetchone()[0]


def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing file: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    for table in _CLEARED:
        assert conn.execute(
            text(f"SELECT COUNT(*) FROM {table}")).fetchone()[0] == 0


# ── the four-part key ────────────────────────────────────────────────────────

def test_a_character_and_a_corporation_with_one_id_stay_apart(conn):
    """`owner_kind` is the half of the key the character tests never vary. Both
    sit at division 0, so without it one overwrites the other."""
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": 1}])
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": 2}],
                                  wallet_api.CORPORATION)
    conn.commit()

    assert _ledger_rows(conn, CORP) == 2, "the two kinds collapsed onto one row"
    personal, _ = wallet_api.load_cached_ledger(conn, CORP, wallet_api.JOURNAL)
    corp, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION)
    assert [r["id"] for r in personal] == [1]
    assert [r["id"] for r in corp] == [2]


def test_all_four_key_parts_are_needed_at_once(conn):
    """Four rows differing in exactly one part each. Any single part missing
    from the conflict target collapses one of these pairs, and the survivor
    looks like a perfectly ordinary wallet."""
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": "base"}],
                                  wallet_api.CORPORATION, 1)
    wallet_api.save_cached_ledger(conn, CORP + 1, wallet_api.JOURNAL, [{"id": "owner"}],
                                  wallet_api.CORPORATION, 1)
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": "kind"}],
                                  wallet_api.CHARACTER, 1)
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": "division"}],
                                  wallet_api.CORPORATION, 2)
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.TRANSACTIONS, [{"id": "ledger"}],
                                  wallet_api.CORPORATION, 1)
    conn.commit()

    def _get(owner, kind, div, ledger):
        rows, _ = wallet_api.load_cached_ledger(conn, owner, ledger, kind, div)
        return rows[0]["id"]

    assert _get(CORP, wallet_api.CORPORATION, 1, wallet_api.JOURNAL) == "base"
    assert _get(CORP + 1, wallet_api.CORPORATION, 1, wallet_api.JOURNAL) == "owner"
    assert _get(CORP, wallet_api.CHARACTER, 1, wallet_api.JOURNAL) == "kind"
    assert _get(CORP, wallet_api.CORPORATION, 2, wallet_api.JOURNAL) == "division"
    assert _get(CORP, wallet_api.CORPORATION, 1, wallet_api.TRANSACTIONS) == "ledger"


def test_saving_the_same_key_twice_replaces_rather_than_duplicating(conn):
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": 1}],
                                  wallet_api.CORPORATION, 3)
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": 2}],
                                  wallet_api.CORPORATION, 3)
    conn.commit()

    assert _ledger_rows(conn, CORP) == 1
    rows, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 3)
    assert [r["id"] for r in rows] == [2], "the older save won"


def test_the_second_save_moves_the_age_forward(conn):
    """`cached_at=excluded.cached_at` in the DO UPDATE. Without it the row keeps
    its first timestamp and the page reports fresh money as stale."""
    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.JOURNAL, [{"id": 1}])
    conn.commit()
    _, first = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL)

    conn.execute(
        text("UPDATE wallet_ledger_cache SET cached_at=:at WHERE owner_id=:owner_id"),
        {"at": first - 3600, "owner_id": ALICE})
    conn.commit()

    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.JOURNAL, [{"id": 2}])
    conn.commit()

    _, second = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL)
    assert second > first - 3600, "the upsert kept the old timestamp"


def test_corporation_balances_live_at_division_zero(conn):
    """The documented exception to the division rule: ESI returns every
    division's balance in one response, so they are stored once rather than
    split across seven rows that would have to be written together."""
    wallet_api.save_cached_ledger(
        conn, CORP, wallet_api.BALANCES,
        [{"division": 1, "balance": 10.0}, {"division": 2, "balance": 20.0}],
        wallet_api.CORPORATION)
    conn.commit()

    rows, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.BALANCES, wallet_api.CORPORATION,
        wallet_api.NO_DIVISION)

    assert [r["division"] for r in rows] == [1, 2]


def test_balances_do_not_collide_with_a_division_zero_journal(conn):
    """`ledger` is what separates them, since both sit at division 0."""
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.BALANCES, [{"b": 1}],
                                  wallet_api.CORPORATION)
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"j": 1}],
                                  wallet_api.CORPORATION)
    conn.commit()

    balances, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.BALANCES, wallet_api.CORPORATION)
    journal, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION)
    assert balances == [{"b": 1}]
    assert journal == [{"j": 1}]


def test_the_ledger_writer_does_not_commit(engine):
    """The caller owns the transaction boundary. Both halves matter — only the
    first would also pass if the writer never wrote anything at all."""
    with engine.connect() as writer:
        wallet_api.save_cached_ledger(writer, ALICE, wallet_api.JOURNAL, [{"id": 1}])

        with engine.connect() as reader:
            assert _ledger_rows(reader, ALICE) == 0, (
                "the writer committed — the caller's boundary moved into it")

        writer.commit()

    with engine.connect() as reader:
        assert _ledger_rows(reader, ALICE) == 1, "the caller's commit did not land"


def test_an_empty_ledger_is_still_a_sync(conn):
    wallet_api.save_cached_ledger(conn, ALICE, wallet_api.JOURNAL, [])
    conn.commit()

    rows, at = wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL)
    assert rows == []
    assert at > 0


def test_a_corrupt_ledger_reads_as_never_synced(conn):
    conn.execute(
        text("INSERT INTO wallet_ledger_cache"
             " (owner_id, owner_kind, division, ledger, data_json, cached_at)"
             " VALUES (:owner_id, :kind, :division, :ledger, :data, :cached_at)"),
        {"owner_id": ALICE, "kind": wallet_api.CHARACTER, "division": 0,
         "ledger": wallet_api.JOURNAL, "data": "{not json",
         "cached_at": time.time()})
    conn.commit()

    assert wallet_api.load_cached_ledger(conn, ALICE, wallet_api.JOURNAL) == (None, 0.0)


# ── the balance, which lives in a different table ────────────────────────────

def test_a_trillion_isk_balance_survives_to_the_cent(conn):
    """ISK runs to trillions. A column that quietly became single precision
    would round this and still return something that looks like a balance."""
    wallet_api.save_cached_balance(conn, ALICE, BIG_ISK)
    conn.commit()

    balance, _ = wallet_api.load_cached_balance(conn, ALICE)

    assert balance == BIG_ISK


def test_saving_a_balance_twice_replaces_rather_than_duplicating(conn):
    wallet_api.save_cached_balance(conn, ALICE, 100.0)
    wallet_api.save_cached_balance(conn, ALICE, 200.0)
    conn.commit()

    assert _balance_rows(conn, ALICE) == 1
    assert wallet_api.load_cached_balance(conn, ALICE)[0] == 200.0


def test_a_zero_balance_is_not_a_missing_one(conn):
    """A cleaned-out wallet is a real state, and `0.0` is falsy — anything
    testing the balance for truthiness turns a real zero into "not synced"."""
    wallet_api.save_cached_balance(conn, ALICE, 0.0)
    conn.commit()

    balance, at = wallet_api.load_cached_balance(conn, ALICE)

    assert balance == 0.0
    assert balance is not None
    assert at > 0


def test_an_unsynced_balance_reads_as_none(conn):
    assert wallet_api.load_cached_balance(conn, ALICE) == (None, 0.0)


def test_one_characters_balance_does_not_disturb_anothers(conn):
    wallet_api.save_cached_balance(conn, ALICE, 100.0)
    wallet_api.save_cached_balance(conn, ALICE + 1, 200.0)
    conn.commit()

    assert wallet_api.load_cached_balance(conn, ALICE)[0] == 100.0
    assert wallet_api.load_cached_balance(conn, ALICE + 1)[0] == 200.0


def test_the_balance_writer_does_not_commit(engine):
    with engine.connect() as writer:
        wallet_api.save_cached_balance(writer, ALICE, 100.0)

        with engine.connect() as reader:
            assert _balance_rows(reader, ALICE) == 0

        writer.commit()

    with engine.connect() as reader:
        assert _balance_rows(reader, ALICE) == 1, "the caller's commit did not land"


# ── fetch_corp_wallets: answers with a reason ────────────────────────────────

def test_corp_wallets_are_cached_as_balances_at_division_zero(conn):
    wallets = [{"division": 1, "balance": 10.0}, {"division": 2, "balance": 20.0}]
    client = _Client(_Resp(200, wallets))

    got, err = asyncio.run(wallet_api.fetch_corp_wallets(client, CORP, "tok", conn=conn))
    conn.commit()

    assert err is None
    assert got == wallets
    cached, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.BALANCES, wallet_api.CORPORATION,
        wallet_api.NO_DIVISION)
    assert cached == wallets


def test_corp_wallets_403_names_the_missing_role(conn):
    """The most common failure here and the only one the user can fix. "ESI
    returned HTTP 403" would send them looking for an outage instead."""
    got, err = asyncio.run(wallet_api.fetch_corp_wallets(
        _Client(_Resp(403)), CORP, "tok", conn=conn))

    assert got is None
    assert "Accountant" in err


def test_corp_wallets_other_errors_carry_their_code(conn):
    got, err = asyncio.run(wallet_api.fetch_corp_wallets(
        _Client(_Resp(503)), CORP, "tok", conn=conn))

    assert got is None
    assert "503" in err


def test_corp_wallets_transport_failure_is_reported_not_swallowed(conn):
    got, err = asyncio.run(wallet_api.fetch_corp_wallets(
        _Client(RuntimeError("connection reset")), CORP, "tok", conn=conn))

    assert got is None
    assert "connection reset" in err


def test_a_failed_corp_wallets_fetch_keeps_the_previous_balances(conn):
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.BALANCES, [{"division": 1}],
                                  wallet_api.CORPORATION)
    conn.commit()

    asyncio.run(wallet_api.fetch_corp_wallets(
        _Client(_Resp(403)), CORP, "tok", conn=conn))
    conn.commit()

    kept, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.BALANCES, wallet_api.CORPORATION)
    assert kept == [{"division": 1}]


# ── fetch_corp_journal: the division has to reach the key ────────────────────

def test_a_corp_journal_is_cached_under_its_own_division(conn):
    """The whole reason `division` is in the key. Filed under the wrong one and
    the page shows another division's money — which looks entirely plausible."""
    client = _Client(_Resp(200, [{"id": 7}]))

    got = asyncio.run(wallet_api.fetch_corp_journal(
        client, CORP, 3, "tok", conn=conn))
    conn.commit()

    assert [r["id"] for r in got] == [7]
    cached, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 3)
    assert [r["id"] for r in cached] == [7]
    assert wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 1) == (None, 0.0)


def test_a_corp_journal_is_not_filed_under_the_character_kind(conn):
    asyncio.run(wallet_api.fetch_corp_journal(
        _Client(_Resp(200, [{"id": 7}])), CORP, 3, "tok", conn=conn))
    conn.commit()

    assert wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CHARACTER, 3) == (None, 0.0)


def test_the_corp_journal_url_carries_the_division(conn):
    client = _Client(_Resp(200, []))

    asyncio.run(wallet_api.fetch_corp_journal(client, CORP, 5, "tok", conn=conn))

    assert f"/corporations/{CORP}/wallets/5/journal/" in client.urls[0]


def test_a_first_page_failure_leaves_the_corp_journal_alone(conn):
    """`return None` before the save. Cached, an empty journal erases a month
    and the next sync writes the erasure down."""
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.JOURNAL, [{"id": 1}],
                                  wallet_api.CORPORATION, 3)
    conn.commit()

    got = asyncio.run(wallet_api.fetch_corp_journal(
        _Client(_Resp(500)), CORP, 3, "tok", conn=conn))
    conn.commit()

    assert got is None
    kept, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 3)
    assert [r["id"] for r in kept] == [1]


def test_a_later_corp_page_failing_keeps_what_arrived(conn):
    """Deliberately different from page one: a partial month beats none, once
    we know the division has activity."""
    client = _Client(_Resp(200, [{"id": 1}], pages=3), _Resp(500))

    got = asyncio.run(wallet_api.fetch_corp_journal(
        client, CORP, 3, "tok", conn=conn))
    conn.commit()

    assert [r["id"] for r in got] == [1]
    cached, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 3)
    assert [r["id"] for r in cached] == [1]


def test_the_corp_journal_walks_its_pages(conn):
    client = _Client(_Resp(200, [{"id": 1}], pages=3),
                     _Resp(200, [{"id": 2}], pages=3),
                     _Resp(200, [{"id": 3}], pages=3))

    got = asyncio.run(wallet_api.fetch_corp_journal(
        client, CORP, 3, "tok", conn=conn))

    assert [r["id"] for r in got] == [1, 2, 3]
    assert [p.get("page") for p in client.params] == [1, 2, 3]


def test_the_corp_journal_stops_at_the_limit(conn):
    client = _Client(*[_Resp(200, [{"id": i}, {"id": i + 100}], pages=9)
                       for i in range(9)])

    got = asyncio.run(wallet_api.fetch_corp_journal(
        client, CORP, 3, "tok", limit=4, conn=conn))

    assert len(got) >= 4
    assert client.calls == 2, "it kept paging past the limit"


def test_the_corp_journal_is_bounded_even_if_esi_reports_nonsense(conn):
    client = _Client(*[_Resp(200, [{"id": i}], pages=9999) for i in range(50)])

    asyncio.run(wallet_api.fetch_corp_journal(
        client, CORP, 3, "tok", limit=10_000, conn=conn))

    assert client.calls == wallet_api._MAX_JOURNAL_PAGES


# ── fetch_corp_transactions ──────────────────────────────────────────────────

def test_corp_transactions_are_cached_under_their_division(conn):
    client = _Client(_Resp(200, [{"transaction_id": 9}]))

    got = asyncio.run(wallet_api.fetch_corp_transactions(
        client, CORP, 4, "tok", conn=conn))
    conn.commit()

    assert [r["transaction_id"] for r in got] == [9]
    cached, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.TRANSACTIONS, wallet_api.CORPORATION, 4)
    assert [r["transaction_id"] for r in cached] == [9]


def test_corp_transactions_do_not_overwrite_the_same_divisions_journal(conn):
    """`ledger` separates them, and the worker writes both back to back."""
    asyncio.run(wallet_api.fetch_corp_journal(
        _Client(_Resp(200, [{"id": "j"}])), CORP, 4, "tok", conn=conn))
    asyncio.run(wallet_api.fetch_corp_transactions(
        _Client(_Resp(200, [{"id": "t"}])), CORP, 4, "tok", conn=conn))
    conn.commit()

    journal, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 4)
    txns, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.TRANSACTIONS, wallet_api.CORPORATION, 4)
    assert [r["id"] for r in journal] == ["j"]
    assert [r["id"] for r in txns] == ["t"]


@pytest.mark.parametrize("failure", [_Resp(500), RuntimeError("reset")],
                         ids=["error-status", "transport"])
def test_a_failed_corp_transactions_fetch_writes_nothing(conn, failure):
    wallet_api.save_cached_ledger(conn, CORP, wallet_api.TRANSACTIONS, [{"id": 1}],
                                  wallet_api.CORPORATION, 4)
    conn.commit()

    got = asyncio.run(wallet_api.fetch_corp_transactions(
        _Client(failure), CORP, 4, "tok", conn=conn))
    conn.commit()

    assert got is None
    kept, _ = wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.TRANSACTIONS, wallet_api.CORPORATION, 4)
    assert [r["id"] for r in kept] == [1]


@pytest.mark.parametrize("fetcher, args", [
    ("fetch_corp_journal", (CORP, 3)),
    ("fetch_corp_transactions", (CORP, 3)),
])
def test_a_corp_fetch_without_a_connection_still_returns(conn, fetcher, args):
    got = asyncio.run(getattr(wallet_api, fetcher)(
        _Client(_Resp(200, [{"id": 1}])), *args, "tok"))

    assert [r["id"] for r in got] == [1]
    assert wallet_api.load_cached_ledger(
        conn, CORP, wallet_api.JOURNAL, wallet_api.CORPORATION, 3) == (None, 0.0)


# ── the balance fetcher ──────────────────────────────────────────────────────

def test_a_failed_balance_fetch_keeps_the_previous_number(conn):
    """`fetch_balance` swallows everything and returns None. The dashboard
    reads the cache, so a blip must not blank the balance."""
    wallet_api.save_cached_balance(conn, ALICE, BIG_ISK)
    conn.commit()

    got = asyncio.run(wallet_api.fetch_balance(
        _Client(_Resp(500)), ALICE, "tok", conn=conn))
    conn.commit()

    assert got is None
    assert wallet_api.load_cached_balance(conn, ALICE)[0] == BIG_ISK


def test_the_balance_fetcher_sends_the_token(conn):
    client = _Client(_Resp(200, 1.0))

    asyncio.run(wallet_api.fetch_balance(client, ALICE, "tok-abc", conn=conn))

    assert client.headers[0].get("Authorization") == "Bearer tok-abc"
