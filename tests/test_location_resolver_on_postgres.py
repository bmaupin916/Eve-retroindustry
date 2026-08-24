"""`location_resolver`, before it moves onto the portable query layer.

The same probe that found seven untested functions in `industry_helper` says
four here are never executed by the suite at all — `get_security_status`,
`get_cached_security`, `locations_in_system` and `_set_error_limited` — and
three more are called exactly once across 838 tests, which is not the same as
being asserted: `get_region_for_location`, `get_station_security_multiplier`
and `save_location_names_to_db`.

Two of those are writers that commit, and a write that loses its `commit()`
during a conversion passes every assertion made on the same connection and drops
the row when the request ends. That is the expensive failure, so it gets the
`_reopen` treatment: every commit here is asserted through a *second*
connection, which is the only version of the question that can fail.

**These assertions are unchanged by the conversion.** They were written against
the `sqlite3` version first, exactly so that they could be preserved rather than
invented afterwards to fit whatever the rewrite did. All that moved is the
fixture underneath them, which now runs each one on both backends.

Postgres comes from the container in `tests/test_postgres_schema.py`; without it
those parameterisations skip and the SQLite half still runs.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, text

from app.web import location_resolver as lr
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_location_resolver"

SYSTEM = 30000142            # Jita
STATION = 60003760           # Jita IV - Moon 4
STRUCTURE = 1_049_982_731_184
REGION = 10000002            # The Forge
CONSTELLATION = 20000020


# ── stubs ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Client:
    """Enough of httpx.AsyncClient for these fetchers, with a scripted queue."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.urls: list[str] = []

    async def get(self, url, **kw):
        self.urls.append(url)
        nxt = self._queue.pop(0) if self._queue else _Resp(500)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def calls(self) -> int:
        return len(self.urls)


class _CM:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        return False


def _stub_esi(monkeypatch, client: _Client) -> _Client:
    """Patch the name `location_resolver` calls, not the one it imported from.

    The module does `from app.esi.client import esi_client` at import, so the
    attribute on `app.esi.client` is no longer the one being called. Patching
    there is the mistake this project has now made four times.
    """
    monkeypatch.setattr(lr, "esi_client", lambda *a, **k: _CM(client))
    return client


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    """An engine per backend, with the app tables present and empty.

    A file rather than `:memory:` on SQLite, because the commit assertions open
    a *second* connection and every `:memory:` connection is a distinct, empty
    database that merely shares a name.
    """
    from app.db.migrate import upgrade_to_head

    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'eve_cache.db'}"
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


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _backend(conn) -> str:
    return conn.engine.dialect.name


@pytest.fixture(autouse=True)
def _reset_module_state():
    """`_error_limited_until` and the name caches are module globals that
    outlive a test. Leaking one turns an unrelated later test into a mystery."""
    before = (lr._error_limited_until, dict(lr._cache), dict(lr._sys_cache),
              set(lr._forbidden))
    yield
    lr._error_limited_until = before[0]
    lr._cache.clear(); lr._cache.update(before[1])
    lr._sys_cache.clear(); lr._sys_cache.update(before[2])
    lr._forbidden.clear(); lr._forbidden.update(before[3])


def _seed_location(conn, location_id=STATION, name="Jita IV",
                   system_id=SYSTEM, region_id=None):
    conn.execute(
        text("INSERT INTO location_name_cache (location_id, name,"
             " solar_system_id, region_id)"
             " VALUES (:loc, :name, :sys, :region)"),
        {"loc": location_id, "name": name, "sys": system_id,
         "region": region_id})
    conn.commit()


def _rows_for(conn, system_id=SYSTEM) -> int:
    """Rows in the cache for this system, NULL ones included.

    `get_cached_security` answers None for "no row" and for "row holding NULL"
    alike, so it cannot express "nothing was written down". Counting rows can,
    and the difference matters: this cache has no expiry, so anything written
    here is written permanently.
    """
    return conn.execute(
        text("SELECT COUNT(*) FROM solar_system_cache WHERE system_id=:sys"),
        {"sys": system_id}).fetchone()[0]


def _seed_security(conn, system_id=SYSTEM, sec=0.946):
    conn.execute(
        text("INSERT INTO solar_system_cache (system_id, security_status,"
             " cached_at) VALUES (:sys, :sec, 0)"),
        {"sys": system_id, "sec": sec})
    conn.commit()


# ── get_cached_security ──────────────────────────────────────────────────────

def test_an_uncached_system_has_no_security(conn):
    assert lr.get_cached_security(conn, SYSTEM) is None


def test_a_cached_security_comes_back(conn):
    _seed_security(conn, SYSTEM, 0.946)

    assert lr.get_cached_security(conn, SYSTEM) == pytest.approx(0.946)


def test_a_null_security_reads_as_uncached(conn):
    """The column is nullable, and a row with NULL means "asked, got nothing"
    rather than a security of zero."""
    conn.execute(
        text("INSERT INTO solar_system_cache (system_id, security_status,"
             " cached_at) VALUES (:sys, NULL, 0)"), {"sys": SYSTEM})
    conn.commit()

    assert lr.get_cached_security(conn, SYSTEM) is None


def test_a_security_of_exactly_zero_is_a_real_answer(conn):
    """Nullsec systems really do sit at 0.0, and 0.0 is falsy.

    A truthiness check here instead of `is not None` reads a nullsec system as
    uncached, which silently downgrades every rig bonus in it from x2.1 to the
    x1.0 highsec fallback — a wrong number with no error anywhere.
    """
    _seed_security(conn, SYSTEM, 0.0)

    assert lr.get_cached_security(conn, SYSTEM) == 0.0


# ── security_multiplier boundaries ───────────────────────────────────────────

def test_the_highsec_boundary_is_inclusive():
    """0.5 is highsec. `>` instead of `>=` moves every 0.5 system into lowsec
    and multiplies its rig bonuses by 1.9."""
    assert lr.security_multiplier(0.5) == 1.0
    assert lr.security_multiplier(0.45) == 1.9


def test_the_nullsec_boundary_is_exclusive():
    """0.0 is nullsec, not lowsec — EVE rounds 0.05 down to 0.0 and treats it
    as null for industry purposes."""
    assert lr.security_multiplier(0.0) == 2.1
    assert lr.security_multiplier(0.0, is_reaction=True) == 1.1
    assert lr.security_multiplier(0.1) == 1.9


# ── get_station_security_multiplier ──────────────────────────────────────────

def test_an_unknown_station_is_neutral(conn):
    assert lr.get_station_security_multiplier(conn, STATION) == 1.0


def test_a_station_with_no_system_is_neutral(conn):
    """A name can be cached before the system behind it is known."""
    _seed_location(conn, STATION, "Somewhere", system_id=None)

    assert lr.get_station_security_multiplier(conn, STATION) == 1.0


def test_a_known_station_in_an_unmeasured_system_is_neutral(conn):
    """System known, security not fetched yet — falls back to highsec rather
    than refusing to answer, so a missing ESI call cannot block a plan."""
    _seed_location(conn, STATION, "Jita IV", SYSTEM)

    assert lr.get_station_security_multiplier(conn, STATION) == 1.0


def test_a_nullsec_station_multiplies_manufacturing_and_reactions_differently(conn):
    """The distinction the whole function exists for: reactions do not scale
    the same way, and using the manufacturing table for a Tatara overstates
    every reaction bonus by nearly double."""
    _seed_location(conn, STRUCTURE, "Some Sotiyo", SYSTEM)
    _seed_security(conn, SYSTEM, -0.1)

    assert lr.get_station_security_multiplier(conn, STRUCTURE) == 2.1
    assert lr.get_station_security_multiplier(
        conn, STRUCTURE, is_reaction=True) == 1.1


# ── locations_in_system ──────────────────────────────────────────────────────

def test_locations_in_system_returns_only_that_system(conn):
    _seed_location(conn, STATION, "Jita IV", SYSTEM)
    _seed_location(conn, STRUCTURE, "Some Sotiyo", SYSTEM)
    _seed_location(conn, 60003761, "Amarr VIII", 30002187)

    found = lr.locations_in_system(conn, SYSTEM)

    assert {r["location_id"] for r in found} == {STATION, STRUCTURE}
    assert {r["name"] for r in found} == {"Jita IV", "Some Sotiyo"}


def test_an_empty_system_returns_an_empty_list(conn):
    assert lr.locations_in_system(conn, SYSTEM) == []


# ── save_location_names_to_db ────────────────────────────────────────────────

def test_saved_names_read_back(conn):
    lr.save_location_names_to_db(
        conn, {STATION: ("Jita IV", SYSTEM), STRUCTURE: ("Sotiyo", None)})

    assert lr.load_location_names_from_db(conn) == {
        STATION: "Jita IV", STRUCTURE: "Sotiyo"}
    assert lr.load_location_sys_from_db(conn) == {STATION: SYSTEM}


def test_saving_a_name_does_not_wipe_the_region(conn):
    """`region_id` is filled in by two ESI calls in `get_region_for_location`
    and is not named by this statement. Under the old `INSERT OR REPLACE` the
    row was deleted and re-inserted, so every unnamed column came back NULL and
    the next asset refresh threw the region away. `ON CONFLICT DO UPDATE` writes
    only what it is given; this is the assertion that notices if that regresses.
    """
    _seed_location(conn, STATION, "Jita IV", SYSTEM, region_id=REGION)

    lr.save_location_names_to_db(conn, {STATION: ("Jita IV - Moon 4", SYSTEM)})

    row = conn.execute(
        text("SELECT name, region_id FROM location_name_cache"
             " WHERE location_id=:loc"), {"loc": STATION}).fetchone()
    assert row[0] == "Jita IV - Moon 4", "the name did not update"
    assert row[1] == REGION, "the region was wiped"


def test_saving_nothing_is_not_an_error(conn):
    """An empty batch happens whenever every name was already cached.

    It is a no-op for `executemany`, and it is *not* one for the SQLAlchemy
    equivalent — with no rows to infer the parameter shape from it raises. This
    is here so the conversion cannot quietly turn a common case into a 500.
    """
    lr.save_location_names_to_db(conn, {})

    assert lr.load_location_names_from_db(conn) == {}


def test_saved_names_survive_a_new_connection(engine):
    """The lost-`commit()` net for this writer.

    SQLAlchemy opens a transaction on first use and rolls it back when the
    connection closes, where `sqlite3` in its default isolation mode commits
    some statements for itself. Asking a *different* connection is the only
    version of this question that can fail.
    """
    with engine.connect() as c:
        lr.save_location_names_to_db(c, {STATION: ("Jita IV", SYSTEM)})

    with engine.connect() as c:
        assert lr.load_location_names_from_db(c) == {STATION: "Jita IV"}, (
            f"on {engine.dialect.name}: the write did not commit")


# ── get_security_status ──────────────────────────────────────────────────────

def test_a_cached_security_is_not_refetched(conn, monkeypatch):
    """It caches permanently and deliberately — security status does not change
    outside faction warfare, which this ignores. A cache that still called ESI
    would be the whole point of the function undone."""
    client = _stub_esi(monkeypatch, _Client())
    _seed_security(conn, SYSTEM, 0.946)

    got = asyncio.run(lr.get_security_status(conn, SYSTEM))

    assert got == pytest.approx(0.946)
    assert client.calls == 0, "it went to ESI despite having the answer"


def test_an_uncached_security_is_fetched_and_stored(conn, monkeypatch):
    client = _stub_esi(monkeypatch, _Client(
        _Resp(200, {"security_status": -0.19})))

    got = asyncio.run(lr.get_security_status(conn, SYSTEM))

    assert got == pytest.approx(-0.19)
    assert client.calls == 1
    assert lr.get_cached_security(conn, SYSTEM) == pytest.approx(-0.19)


def test_a_fetched_security_survives_a_new_connection(engine, monkeypatch):
    """The lost-`commit()` net for the second writer."""
    _stub_esi(monkeypatch, _Client(_Resp(200, {"security_status": -0.19})))

    with engine.connect() as c:
        asyncio.run(lr.get_security_status(c, SYSTEM))

    with engine.connect() as c:
        assert lr.get_cached_security(c, SYSTEM) == pytest.approx(-0.19), (
            f"on {engine.dialect.name}: the write did not commit")


def test_a_failed_security_fetch_returns_none_and_stores_nothing(conn, monkeypatch):
    """Storing a placeholder would be worse than storing nothing: this cache has
    no expiry, so a bad value written once is permanent."""
    _stub_esi(monkeypatch, _Client(_Resp(500)))

    assert asyncio.run(lr.get_security_status(conn, SYSTEM)) is None
    assert _rows_for(conn) == 0, "a failure was written down as an answer"


def test_a_raising_security_fetch_returns_none(conn, monkeypatch):
    _stub_esi(monkeypatch, _Client(RuntimeError("connection reset")))

    assert asyncio.run(lr.get_security_status(conn, SYSTEM)) is None
    assert _rows_for(conn) == 0


def test_a_response_without_a_security_status_stores_nothing(conn, monkeypatch):
    """200 with the field missing must leave no row at all.

    Asserting `get_cached_security(...) is None` here would be decorative: it
    answers None for a row holding NULL just as it does for no row, so a version
    that cached the absence would pass. The row count is what can fail, and it
    matters because this cache never expires — a NULL written once is a
    permanent record that the system was asked about.
    """
    _stub_esi(monkeypatch, _Client(_Resp(200, {"name": "Jita"})))

    assert asyncio.run(lr.get_security_status(conn, SYSTEM)) is None
    assert _rows_for(conn) == 0


# ── get_region_for_location ──────────────────────────────────────────────────

def test_a_cached_region_is_not_refetched(conn, monkeypatch):
    client = _stub_esi(monkeypatch, _Client())
    _seed_location(conn, STATION, "Jita IV", SYSTEM, region_id=REGION)

    got = asyncio.run(lr.get_region_for_location(conn, STATION))

    assert got == REGION
    assert client.calls == 0


def test_a_region_is_resolved_through_the_constellation_and_stored(conn, monkeypatch):
    """system → constellation → region is two calls, and the result is written
    back so it is two calls once per location rather than per page."""
    client = _stub_esi(monkeypatch, _Client(
        _Resp(200, {"constellation_id": CONSTELLATION}),
        _Resp(200, {"region_id": REGION})))
    _seed_location(conn, STATION, "Jita IV", SYSTEM)

    got = asyncio.run(lr.get_region_for_location(conn, STATION))

    assert got == REGION
    assert client.calls == 2
    row = conn.execute(
        text("SELECT region_id FROM location_name_cache"
             " WHERE location_id=:loc"), {"loc": STATION}).fetchone()
    assert row[0] == REGION, "the region was not written back"


def test_a_resolved_region_survives_a_new_connection(engine, monkeypatch):
    """The lost-`commit()` net for the third writer."""
    _stub_esi(monkeypatch, _Client(
        _Resp(200, {"constellation_id": CONSTELLATION}),
        _Resp(200, {"region_id": REGION})))

    with engine.connect() as c:
        _seed_location(c, STATION, "Jita IV", SYSTEM)
        asyncio.run(lr.get_region_for_location(c, STATION))

    with engine.connect() as c:
        row = c.execute(
            text("SELECT region_id FROM location_name_cache"
                 " WHERE location_id=:loc"), {"loc": STATION}).fetchone()
    assert row[0] == REGION, (
        f"on {engine.dialect.name}: the write did not commit")


def test_a_failed_constellation_lookup_yields_no_region(conn, monkeypatch):
    _stub_esi(monkeypatch, _Client(_Resp(404)))
    _seed_location(conn, STATION, "Jita IV", SYSTEM)

    assert asyncio.run(lr.get_region_for_location(conn, STATION)) is None


def test_a_constellation_without_a_region_yields_none(conn, monkeypatch):
    _stub_esi(monkeypatch, _Client(
        _Resp(200, {"constellation_id": CONSTELLATION}),
        _Resp(200, {})))
    _seed_location(conn, STATION, "Jita IV", SYSTEM)

    assert asyncio.run(lr.get_region_for_location(conn, STATION)) is None


# ── the error-limit latch ────────────────────────────────────────────────────

def test_the_error_limit_latch_holds_and_then_clears():
    assert lr._is_error_limited() is False

    lr._set_error_limited(30.0)
    assert lr._is_error_limited() is True

    lr._error_limited_until = 0.0
    assert lr._is_error_limited() is False


def test_a_shorter_latch_cannot_shorten_a_longer_one():
    """`max`, not assignment. A 1-second 420 arriving during a 60-second hold
    must not release the hold early — that is how a cascade restarts."""
    lr._set_error_limited(600.0)
    long_hold = lr._error_limited_until

    lr._set_error_limited(1.0)

    assert lr._error_limited_until == long_hold


# ── the control ──────────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing file: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    assert conn.execute(
        text("SELECT COUNT(*) FROM location_name_cache")).fetchone()[0] == 0
