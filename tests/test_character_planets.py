"""`app/character/planets.py`, before it moves onto the portable query layer.

`tests/test_orders_cache.py` already pins the interesting part of this module —
the three-way result of `load_cached_colonies` (`(colonies, details)` /
`"forbidden"` / `None`), that "forbidden" is cached because it is a durable
fact about the token rather than a transient failure, that a failed detail call
leaves `None` in its *slot* rather than dropping the colony, and that an empty
colony list is a real sync. `tests/test_pi_planner.py` covers `fetch_planets`
through the planner. None of that is repeated here.

**`fetch_planet_names` has no test**, and it is the only writer of
`planet_name_cache`. That matters for the conversion twice over: it holds the
module's one chunked `IN (...)` — the pattern that has to become an expanding
bindparam — and it is an upsert whose transaction boundary belongs to its
caller.

**These assertions are written against the `sqlite3` version on purpose**, so
the conversion is judged by whether it preserves them.

Three things here are conversion traps rather than ordinary behaviour:

* **`load_planet_names` chunks its `IN (...)` at 900.** A statement caps how
  many parameters it may bind, and an expanding bindparam still binds one per
  id. The cap is a compile-time setting, so the chunking has to survive the
  rewrite even though the build here would tolerate its absence — see the
  matching note in `tests/test_character_assets_on_postgres.py`, where a test
  named a threshold that had moved.
* **Names are permanent and fetched at most once per database, ever.** So
  `fetch_planet_names` consults the cache first and only calls ESI for what is
  missing. A rewrite that lost that turns a first-sight-only cost into a
  per-tick one, silently.
* **A planet whose name fails to resolve is not written.** It renders as its
  id and gets another chance next time; caching a null would make one bad
  round trip permanent, because nothing ever refreshes this table.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.character import planets as planets_api
from app.db.schema import apply_schema

ALICE = 2_112_625_428


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Client:
    """Routes by planet id in the URL, because `fetch_planet_names` issues its
    calls concurrently — a positional queue would make the test depend on the
    order `asyncio.gather` happens to resolve in."""

    def __init__(self, by_id: dict):
        self.by_id = by_id
        self.urls: list[str] = []

    async def get(self, url, **kw):
        self.urls.append(url)
        pid = int(url.rstrip("/").rsplit("/", 1)[-1])
        nxt = self.by_id.get(pid, _Resp(404))
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def calls(self) -> int:
        return len(self.urls)


def _named(name: str) -> _Resp:
    return _Resp(200, {"name": name})


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "planets.db"))
    apply_schema(c)
    yield c
    c.close()


def _cached_names(conn) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT planet_id, name FROM planet_name_cache").fetchall()}


# ── load_planet_names ────────────────────────────────────────────────────────

def test_no_ids_asks_nothing(conn):
    """The empty-batch guard. Without it the statement is `IN ()`, which is a
    syntax error rather than an empty result."""
    assert planets_api.load_planet_names(conn, []) == {}


def test_only_known_names_come_back(conn):
    conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                 (4001, "Testworld IV"))
    conn.commit()

    assert planets_api.load_planet_names(conn, [4001, 4002]) == {4001: "Testworld IV"}


def test_a_null_name_is_not_a_name(conn):
    """The column is nullable and the reader skips falsy values. A planet with
    a NULL name must read as unknown, so it renders as its id and can be
    resolved again — not as an empty string in the UI."""
    conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                 (4001, None))
    conn.commit()

    assert planets_api.load_planet_names(conn, [4001]) == {}


def test_ids_are_coerced_from_strings(conn):
    """They arrive from JSON and from query strings. `int(p)` in the reader is
    what makes both work; without it a string id matches nothing."""
    conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                 (4001, "Testworld IV"))
    conn.commit()

    assert planets_api.load_planet_names(conn, ["4001"]) == {4001: "Testworld IV"}


def test_more_ids_than_one_chunk(conn):
    """Chunked at 900. A twelve-character account with colonies everywhere is
    nowhere near that, but the chunking is what keeps the statement inside the
    parameter cap on builds where the cap is low — and an expanding bindparam
    still binds one parameter per id."""
    ids = list(range(5000, 5000 + 1500))
    conn.executemany(
        "INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
        [(pid, f"P{pid}") for pid in ids])
    conn.commit()

    got = planets_api.load_planet_names(conn, ids)

    assert len(got) == 1500
    assert got[5000] == "P5000"
    assert got[6499] == "P6499"


# ── fetch_planet_names ───────────────────────────────────────────────────────

def test_a_known_name_costs_no_round_trip(conn):
    """Names are permanent, so a name is fetched at most once per database
    ever. A rewrite that lost the cache-first check turns that into a call
    every tick, silently."""
    conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                 (4001, "Testworld IV"))
    conn.commit()
    client = _Client({})

    got = asyncio.run(planets_api.fetch_planet_names(client, conn, [4001]))

    assert client.calls == 0, "a cached name still cost an ESI call"
    assert got == {4001: "Testworld IV"}


def test_only_the_missing_ones_are_fetched(conn):
    conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                 (4001, "Testworld IV"))
    conn.commit()
    client = _Client({4002: _named("Testworld V")})

    got = asyncio.run(planets_api.fetch_planet_names(client, conn, [4001, 4002]))
    conn.commit()

    assert client.calls == 1
    assert got == {4001: "Testworld IV", 4002: "Testworld V"}


def test_a_fetched_name_is_stored(conn):
    client = _Client({4002: _named("Testworld V")})

    asyncio.run(planets_api.fetch_planet_names(client, conn, [4002]))
    conn.commit()

    assert _cached_names(conn) == {4002: "Testworld V"}


def test_a_stored_name_is_not_fetched_again(conn):
    """The whole point of the table. Two calls, one round trip."""
    client = _Client({4002: _named("Testworld V")})

    asyncio.run(planets_api.fetch_planet_names(client, conn, [4002]))
    conn.commit()
    asyncio.run(planets_api.fetch_planet_names(client, conn, [4002]))

    assert client.calls == 1


def test_a_failed_resolution_is_not_cached(conn):
    """Nothing ever refreshes this table, so a null written here would make one
    bad round trip permanent. An unresolved planet renders as its id and gets
    another chance next time instead."""
    client = _Client({4002: _Resp(404)})

    got = asyncio.run(planets_api.fetch_planet_names(client, conn, [4002]))
    conn.commit()

    assert got == {}
    assert _cached_names(conn) == {}


def test_a_transport_failure_is_not_cached(conn):
    client = _Client({4002: RuntimeError("connection reset")})

    got = asyncio.run(planets_api.fetch_planet_names(client, conn, [4002]))
    conn.commit()

    assert got == {}
    assert _cached_names(conn) == {}


def test_one_planet_failing_does_not_lose_the_others(conn):
    """The calls run concurrently through `asyncio.gather`. One bad id must
    not cost the whole batch — six colonies set up at once is exactly when
    this path runs."""
    client = _Client({
        4001: _named("Testworld IV"),
        4002: _Resp(500),
        4003: _named("Testworld VI"),
    })

    got = asyncio.run(planets_api.fetch_planet_names(client, conn, [4001, 4002, 4003]))
    conn.commit()

    assert got == {4001: "Testworld IV", 4003: "Testworld VI"}
    assert _cached_names(conn) == {4001: "Testworld IV", 4003: "Testworld VI"}


def test_the_name_writer_does_not_commit(conn, tmp_path):
    """The caller owns the transaction boundary — `fetch_colonies` commits for
    it. Both halves matter: only the first would also pass if nothing had been
    written at all."""
    client = _Client({4002: _named("Testworld V")})

    asyncio.run(planets_api.fetch_planet_names(client, conn, [4002]))

    other = sqlite3.connect(str(tmp_path / "planets.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM planet_name_cache").fetchone()[0] == 0, (
            "the writer committed — the caller's boundary moved into it")
    finally:
        other.close()

    conn.commit()
    other = sqlite3.connect(str(tmp_path / "planets.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM planet_name_cache").fetchone()[0] == 1, (
            "the caller's commit did not land")
    finally:
        other.close()


def test_a_renamed_planet_overwrites_rather_than_duplicating(conn):
    """`ON CONFLICT (planet_id) DO UPDATE`. Planets do not get renamed, but the
    upsert is what makes a re-run idempotent rather than a constraint error."""
    conn.execute("INSERT INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                 (4002, None))
    conn.commit()
    client = _Client({4002: _named("Testworld V")})

    asyncio.run(planets_api.fetch_planet_names(client, conn, [4002]))
    conn.commit()

    assert _cached_names(conn) == {4002: "Testworld V"}
    assert conn.execute(
        "SELECT COUNT(*) FROM planet_name_cache WHERE planet_id=?",
        (4002,)).fetchone()[0] == 1


def test_no_ids_at_all_asks_nothing(conn):
    client = _Client({})

    assert asyncio.run(planets_api.fetch_planet_names(client, conn, [])) == {}
    assert client.calls == 0
