"""The route-jump cache in `app/web/routers/assets.py`, before it moves onto the
portable query layer.

`load_route_jumps` and `save_route_jumps` have no test at all, and they are the
most interesting SQL in that file: a **row-value `IN`** over
`(sys_a, sys_b)` pairs, chunked, against a table whose key is stored
*normalised* because the gate network is undirected.

What the cache is for: jump counts are static — stargates do not move — so a
route's length changes only when CCP edits the map. Without this cache
`/api/assets/distances` fired one ESI call per unique destination system, 482
of them on the developer's own account, on every single request.

**These assertions are written against the `sqlite3` version on purpose**, so
the conversion is judged by whether it preserves them.

Three things here are conversion traps rather than ordinary behaviour:

* **The pair is stored `(min, max)`, not `(origin, dest)`.** A → B and B → A
  are the same route, so normalising halves the table and makes the cache hit
  regardless of which end you ask from. Both the reader and the writer must
  apply the same normalisation, and a conversion that changes one and not the
  other produces a cache that never hits — silently, since a miss just costs
  an ESI call.
* **`WHERE (sys_a, sys_b) IN ((?,?), …)`** is a row-value comparison. SQLAlchemy
  expands a list of *tuples* for it, checked on both backends before the
  rewrite rather than assumed.
* **The `IN` list is chunked**, for the same parameter-cap reason as everywhere
  else — but here each destination costs **two** parameters, not one. The chunk
  was 900, which bound 1,800 parameters against the 999 limit its own comment
  cited: the chunk counted destinations and the cap counts parameters. That was
  a live bug, found by these tests and fixed to 450 in the same commit; the
  arithmetic is pinned below so it cannot drift back.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.web.routers.assets import _ROUTE_CHUNK, load_route_jumps, save_route_jumps

#: The lowest `SQLITE_LIMIT_VARIABLE_NUMBER` still in the wild — the default
#: before SQLite 3.32. The build running these tests allows 32,766.
OLD_SQLITE_VAR_CAP = 999

JITA = 30000142
AMARR = 30002187
DODIXIE = 30002659
RENS = 30002510


@pytest.fixture
def conn(tmp_path):
    from app.db.migrate import upgrade_to_head

    path = tmp_path / "routes.db"
    upgrade_to_head(f"sqlite:///{path}")
    c = sqlite3.connect(str(path))
    yield c
    c.close()


def _rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT sys_a, sys_b, jumps FROM route_jump_cache ORDER BY sys_a, sys_b"
    ).fetchall()


# ── the round trip ───────────────────────────────────────────────────────────

def test_saved_jumps_read_back(conn):
    save_route_jumps(conn, JITA, {AMARR: 9, DODIXIE: 5})

    assert load_route_jumps(conn, JITA, [AMARR, DODIXIE]) == {AMARR: 9, DODIXIE: 5}


def test_asking_for_nothing_asks_the_database_nothing(conn):
    """`load_route_jumps(conn, origin, [])` is `{}`.

    The `if not dests: return {}` guard is **redundant**, and this test says so
    rather than pretending otherwise: `range(0, len([]), 900)` is empty, so the
    loop body never runs and the function returns `{}` anyway. Deleting the
    guard fails nothing, which is the correct result and not a gap in the net.

    An earlier version of this docstring claimed the guard prevented an
    `IN ()` syntax error. It does not — no statement is issued at all. The
    guard is a cheap early return, and the behaviour is what is pinned here.
    """
    assert load_route_jumps(conn, JITA, []) == {}


def test_saving_nothing_writes_nothing(conn):
    """`save_route_jumps(conn, origin, {})` writes nothing.

    Its `if not jumps: return` guard is redundant *today* — `sqlite3`'s
    `executemany` treats an empty sequence as a no-op — and becomes
    **load-bearing after the conversion**: SQLAlchemy raises
    `StatementError: A value is required for bind parameter` when handed an
    empty parameter list. Measured, not assumed.

    So a mutation removing this guard is uncaught before the rewrite and caught
    after it, which is the shape to expect. The assertion is on the behaviour,
    which does not change either way.
    """
    save_route_jumps(conn, JITA, {})

    assert _rows(conn) == []


def test_an_unknown_destination_is_absent_not_zero(conn):
    """Absent means "ask ESI"; zero would mean "same system, no jumps". The
    caller does `s not in cached_jumps` to build its to-do list, so a zero
    here would silently suppress the fetch and report every unknown route as
    being on your doorstep."""
    save_route_jumps(conn, JITA, {AMARR: 9})

    got = load_route_jumps(conn, JITA, [AMARR, DODIXIE])

    assert got == {AMARR: 9}
    assert DODIXIE not in got


def test_a_zero_jump_route_is_a_real_answer(conn):
    """Origin and destination in the same system. Zero is falsy, so anything
    testing truthiness rather than membership turns a real answer back into a
    round trip."""
    save_route_jumps(conn, JITA, {JITA: 0})

    assert load_route_jumps(conn, JITA, [JITA]) == {JITA: 0}


# ── the normalised pair ──────────────────────────────────────────────────────

def test_the_pair_is_stored_low_then_high(conn):
    """Not `(origin, dest)`. The gate network is undirected, so storing the
    pair sorted halves the table and lets either end find it."""
    save_route_jumps(conn, AMARR, {JITA: 9})

    assert _rows(conn) == [(min(AMARR, JITA), max(AMARR, JITA), 9)]


def test_a_route_saved_one_way_is_found_the_other(conn):
    """The property the normalisation exists for. Without it the cache holds
    A→B and misses on B→A, so half the lookups pay for an ESI call that the
    answer is already sitting in the table for."""
    save_route_jumps(conn, JITA, {AMARR: 9})

    assert load_route_jumps(conn, AMARR, [JITA]) == {JITA: 9}


def test_saving_the_reverse_updates_rather_than_duplicating(conn):
    """Both directions normalise to the same key, so the second save is an
    upsert on the same row. Two rows for one route would make the answer depend
    on which the reader happened to see first."""
    save_route_jumps(conn, JITA, {AMARR: 9})
    save_route_jumps(conn, AMARR, {JITA: 11})

    assert _rows(conn) == [(min(AMARR, JITA), max(AMARR, JITA), 11)]


def test_the_upsert_refreshes_the_jump_count(conn):
    """`jumps=excluded.jumps`. CCP does edit the map, and a cache that never
    updates would hold a pre-patch route length forever."""
    save_route_jumps(conn, JITA, {AMARR: 9})
    save_route_jumps(conn, JITA, {AMARR: 12})

    assert load_route_jumps(conn, JITA, [AMARR]) == {AMARR: 12}


def test_the_upsert_moves_the_timestamp_forward(conn):
    save_route_jumps(conn, JITA, {AMARR: 9})
    first = conn.execute("SELECT cached_at FROM route_jump_cache").fetchone()[0]

    conn.execute("UPDATE route_jump_cache SET cached_at=?", (first - 3600,))
    conn.commit()
    assert conn.execute("SELECT cached_at FROM route_jump_cache").fetchone()[0] == \
        pytest.approx(first - 3600), "the backdate did not take"

    save_route_jumps(conn, JITA, {AMARR: 9})

    assert conn.execute(
        "SELECT cached_at FROM route_jump_cache").fetchone()[0] > first - 3600


# ── scoping: one origin's routes are not another's ───────────────────────────

def test_routes_from_a_different_origin_are_not_returned(conn):
    """Two origins sharing a destination. With a single origin in the fixture,
    a reader that ignored the pair entirely would look identical — and this
    cache is keyed on *both* systems, not on the destination alone."""
    save_route_jumps(conn, JITA, {DODIXIE: 5})
    save_route_jumps(conn, AMARR, {DODIXIE: 3})

    assert load_route_jumps(conn, JITA, [DODIXIE]) == {DODIXIE: 5}
    assert load_route_jumps(conn, AMARR, [DODIXIE]) == {DODIXIE: 3}


def test_only_the_destinations_asked_for_come_back(conn):
    """Two stored routes, one requested. A `WHERE` that matched everything
    would return both and the caller would skip an ESI call it needs."""
    save_route_jumps(conn, JITA, {AMARR: 9, DODIXIE: 5})

    assert load_route_jumps(conn, JITA, [AMARR]) == {AMARR: 9}


def test_several_destinations_at_once(conn):
    save_route_jumps(conn, JITA, {AMARR: 9, DODIXIE: 5, RENS: 14})

    got = load_route_jumps(conn, JITA, [AMARR, DODIXIE, RENS])

    assert got == {AMARR: 9, DODIXIE: 5, RENS: 14}


# ── chunking: the cap counts parameters, the chunk counts pairs ──────────────

def test_the_chunk_stays_under_the_oldest_variable_cap(conn):
    """Two parameters per destination, so the chunk must be *half* the cap.

    This is the bug this file found: the chunk was 900, which is 1,800
    parameters against the 999 the code's own comment cited as the reason for
    chunking at all. Harmless on this build (cap 32,766) and an
    `OperationalError` on any build shipping the old default.

    The test below reproduces that build and is the real proof. This one states
    the arithmetic, so a future edit that raises the chunk fails on the line
    that explains why rather than on a `too many SQL variables` several
    functions away.
    """
    assert _ROUTE_CHUNK * 2 <= OLD_SQLITE_VAR_CAP, (
        f"a chunk of {_ROUTE_CHUNK} destinations binds {_ROUTE_CHUNK * 2} "
        f"parameters, over the {OLD_SQLITE_VAR_CAP} cap — the chunk counts "
        f"destinations and the cap counts parameters")


def test_more_destinations_than_one_chunk(conn):
    """901 destinations spans several chunks.

    This proves the results are stitched back together across chunks. It does
    **not** prove the chunking is load-bearing — see the test below, which is
    where the parameter cap actually gets exercised.

    482 unique systems was the real measurement on the developer's account, so
    this is above production but the same order.
    """
    dests = list(range(31000000, 31000000 + 901))
    save_route_jumps(conn, JITA, {d: i % 40 for i, d in enumerate(dests)})

    got = load_route_jumps(conn, JITA, dests)

    assert len(got) == 901
    assert got[dests[0]] == 0
    assert got[dests[900]] == 900 % 40


def test_the_chunking_is_what_keeps_the_query_under_the_parameter_cap(conn):
    """The test above passes with the chunking deleted, and that is not a gap
    in it — it is a fact about this build.

    `SQLITE_LIMIT_VARIABLE_NUMBER` here is **32,766**, so 901 destinations at
    two parameters each — 1,802 — is nowhere near it. The cap is a
    *compile-time* setting: 999 before SQLite 3.32, and some distribution
    builds still ship that. The chunking exists for those, and Postgres caps at
    65,535.

    So the limit is lowered to 999 for this connection, which reproduces the
    build the chunking is for at a realistic number of destinations rather than
    needing 16,000 of them. This is the test that caught the chunk being twice
    the size it could safely be — it failed against the shipped 900 and passes
    against 450.

    Same shape as the assets container-name test: **when a test names a
    threshold, check the threshold is real.**
    """
    dests = list(range(31000000, 31000000 + 901))
    save_route_jumps(conn, JITA, {d: 1 for d in dests})

    was = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, OLD_SQLITE_VAR_CAP)
    try:
        got = load_route_jumps(conn, JITA, dests)
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, was)

    assert len(got) == 901, (
        f"the query bound more than {OLD_SQLITE_VAR_CAP} parameters at once — "
        f"the chunking is not doing its job")


def test_a_chunk_boundary_does_not_drop_the_last_pair(conn):
    """Exactly one chunk's worth — the boundary itself, where an off-by-one in
    the slice shows up as one missing route rather than an error."""
    dests = list(range(31000000, 31000000 + _ROUTE_CHUNK))
    save_route_jumps(conn, JITA, {d: 1 for d in dests})

    assert len(load_route_jumps(conn, JITA, dests)) == _ROUTE_CHUNK


# ── the writer's transaction boundary ────────────────────────────────────────

def test_the_writer_commits(conn, tmp_path):
    """Unlike most writers here, this one owns its boundary — its caller closes
    the connection immediately afterwards, so a commit left to the caller would
    be a commit that never happened."""
    save_route_jumps(conn, JITA, {AMARR: 9})

    other = sqlite3.connect(str(tmp_path / "routes.db"))
    try:
        assert other.execute(
            "SELECT COUNT(*) FROM route_jump_cache").fetchone()[0] == 1, (
            "the writer did not commit, and `assets_distances` closes the "
            "connection on the next line")
    finally:
        other.close()


def test_a_failed_lookup_is_never_stored(conn):
    """Not this function's job, but the invariant it depends on: the caller
    filters `j >= 0` before saving, because -1 means the lookup failed or the
    systems are unreachable, and both must stay retryable. Stored, -1 would be
    read back as a real distance and the retry would never happen.

    Pinned here because the filter lives in the caller and the consequence
    lives in the cache — the two are only correct together.
    """
    save_route_jumps(conn, JITA, {AMARR: 9, DODIXIE: -1})

    got = load_route_jumps(conn, JITA, [AMARR, DODIXIE])

    assert got[AMARR] == 9
    assert got.get(DODIXIE) == -1, (
        "the writer stores whatever it is given — if this ever changes, the "
        "caller's `j >= 0` filter is what actually protects the cache")
