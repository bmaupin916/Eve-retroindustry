"""The background market-history fill — `app/market/history_fill.py`.

The table this fills held **zero rows** until now, because the only thing that
ever wrote one was a user opening a price chart. Everything §9.4 wants from the
daily series — volatility, trend, competition — was waiting on this.

Two properties matter more than the arithmetic and both are pinned here:

* **Order.** The budget is twenty types a round, so what comes first is what
  gets filled today. Watchlist, then what is being built, then its inputs.
* **A failed fetch writes nothing.** Storing an empty series under a fresh
  timestamp would read as "this item has never traded" and suppress the retry
  for twenty hours — the same error as conflating "fetch failed" with "no jobs",
  which `fetch_industry_jobs` returning `None` exists to avoid.

Runs on both backends: the candidate query unions three tables and the freshness
check uses an expanding bind, neither of which is worth assuming.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from sqlalchemy import create_engine, text

from app.market import history_fill
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_history_fill"

WATCHED = 34         # in the watchlist
BUILT = 645          # a project plan's product
INPUT = 36           # a shopping-list input
BOTH = 35            # watchlist *and* shopping list, to prove de-duplication
UNRELATED = 44992    # in no source at all


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    """Records what was asked for, so order can be asserted."""

    def __init__(self, payload=None, fail: set[int] | None = None):
        self._payload = payload if payload is not None else [
            {"date": "2026-08-01", "average": 1.0, "lowest": 1.0,
             "highest": 1.0, "volume": 10, "order_count": 3}]
        self._fail = fail or set()
        self.asked: list[int] = []

    async def get(self, url, **kwargs):
        type_id = (kwargs.get("params") or {}).get("type_id")
        self.asked.append(type_id)
        if type_id in self._fail:
            return _Resp(None)          # a body that is not a list -> None
        return _Resp(self._payload)


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    if request.param == "sqlite":
        from app.db.schema import apply_schema

        eng = create_engine(f"sqlite:///{tmp_path / 'eve_cache.db'}")
        with eng.connect() as c:
            apply_schema(c.connection.driver_connection)
        _seed(eng)
        yield eng
        eng.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

    from app.db.migrate import upgrade_to_head

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
    _seed(eng)
    yield eng
    eng.dispose()


def _seed(eng) -> None:
    with eng.connect() as c:
        for type_id in (WATCHED, BOTH):
            c.execute(text("INSERT INTO margin_watchlist (type_id, me, te, added_at)"
                           " VALUES (:t, 10, 20, 0)"), {"t": type_id})
        c.execute(text("INSERT INTO production_projects (id, name, created_at,"
                       " updated_at) VALUES (1, 'p', 0, 0)"))
        c.execute(text("INSERT INTO project_plans (project_id, product_type_id,"
                       " product_name, quantity, me, te, status, plan_json,"
                       " created_at)"
                       " VALUES (1, :t, 'x', 1, 10, 20, 'active', '{}', 0)"),
                  {"t": BUILT})
        for type_id in (INPUT, BOTH):
            c.execute(text("INSERT INTO project_shopping (project_id, type_id, name,"
                           " needed, purchased) VALUES (1, :t, 'y', 1, 0)"),
                      {"t": type_id})
        c.commit()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def test_the_watchlist_comes_before_what_is_being_built(conn):
    """The budget is small, so order decides what gets filled today."""
    assert history_fill.candidate_type_ids(conn) == [WATCHED, BOTH, BUILT, INPUT]


def test_a_type_in_two_sources_is_asked_for_once(conn):
    """BOTH is on the watchlist and the shopping list."""
    candidates = history_fill.candidate_type_ids(conn)
    assert candidates.count(BOTH) == 1


def test_types_nobody_asked_about_are_never_fetched(conn):
    """19,667 types have a market group. This fetches four of them."""
    assert UNRELATED not in history_fill.candidate_type_ids(conn)


def test_fresh_history_is_not_refetched(conn):
    history_fill.store_region_history(conn, history_fill.JITA_REGION, WATCHED, [])
    conn.commit()
    assert WATCHED not in history_fill.types_needing_history(conn)


def test_history_past_its_ttl_comes_due_again(conn):
    conn.execute(
        text("INSERT INTO price_history_cache (region_id, type_id, data_json,"
             " cached_at) VALUES (:r, :t, '[]', :c)"),
        {"r": history_fill.JITA_REGION, "t": WATCHED,
         "c": time.time() - history_fill.HISTORY_FILL_TTL - 1})
    conn.commit()
    assert WATCHED in history_fill.types_needing_history(conn)


def test_the_budget_is_a_limit_not_a_suggestion(conn):
    assert len(history_fill.types_needing_history(conn, limit=2)) == 2


def test_a_settled_install_asks_for_nothing(conn):
    for type_id in history_fill.candidate_type_ids(conn):
        history_fill.store_region_history(conn, history_fill.JITA_REGION, type_id, [])
    conn.commit()
    assert history_fill.types_needing_history(conn) == []


def test_the_fill_stores_what_it_fetched_and_stores_it_once(conn):
    client = _Client()
    stored = asyncio.run(history_fill.fill_history(client, conn))
    assert stored == 4
    assert client.asked == [WATCHED, BOTH, BUILT, INPUT]

    rows = conn.execute(text("SELECT COUNT(*) FROM price_history_cache")).scalar()
    assert rows == 4
    # And it went in through the producer, so the keys are the producer's.
    raw = conn.execute(
        text("SELECT data_json FROM price_history_cache WHERE type_id = :t"),
        {"t": WATCHED}).scalar()
    assert set(json.loads(raw)[0]) == {"d", "avg", "low", "high", "vol", "orders"}


def test_a_failed_fetch_writes_nothing_and_stays_due(conn):
    """The assertion that keeps a transient failure from looking permanent.

    An empty series under a fresh timestamp reads as "never traded" and
    suppresses the retry for twenty hours.
    """
    client = _Client(fail={WATCHED})
    stored = asyncio.run(history_fill.fill_history(client, conn))

    assert stored == 3                                  # the other three
    assert WATCHED in history_fill.types_needing_history(conn)
    present = {r[0] for r in conn.execute(
        text("SELECT type_id FROM price_history_cache"))}
    assert WATCHED not in present


def test_the_fill_commits_so_the_next_round_sees_it(conn, engine):
    """A write that never commits passes every same-connection assertion."""
    asyncio.run(history_fill.fill_history(_Client(), conn))
    with engine.connect() as other:
        assert other.execute(
            text("SELECT COUNT(*) FROM price_history_cache")).scalar() == 4


# ── the worker phase ─────────────────────────────────────────────────────────

def test_the_worker_phase_survives_a_broken_fill(monkeypatch):
    """The fill is the least important thing the worker does.

    Every KPI it feeds degrades to "not enough history yet". A character whose
    assets and jobs stop syncing because a public market endpoint returned
    something unexpected is a much worse outcome, so the phase swallows and
    reports rather than propagating.
    """
    from app.sync import worker as w

    async def _boom(*a, **kw):
        raise RuntimeError("ESI said no")

    monkeypatch.setattr(history_fill, "types_needing_history",
                        lambda *a, **kw: [WATCHED])
    monkeypatch.setattr(history_fill, "fill_history", _boom)

    # No hasattr guard: a renamed class should fail this test, not skip it.
    # A test that quietly skips itself is indistinguishable from one that passed.
    assert asyncio.run(w.SyncWorker()._fill_market_history()) == 0


def test_a_settled_install_makes_no_esi_call_at_all(monkeypatch):
    """Nothing due must mean no client is opened, not an empty fetch loop.

    Opening an ESI client every tick to discover there is nothing to do is a
    connection and a log line per round, for ever.
    """
    from app.sync import worker as w

    monkeypatch.setattr(history_fill, "types_needing_history", lambda *a, **kw: [])

    def _no_client(*a, **kw):             # pragma: no cover — must not run
        raise AssertionError("opened an ESI client with nothing due")

    monkeypatch.setattr(w, "esi_client", _no_client)
    sync = w.SyncWorker()
    assert asyncio.run(sync._fill_market_history()) == 0
