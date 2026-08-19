"""`app_defaults` is converted. This runs it on both backends.

It is a small module — one read, one upsert — and it sits underneath more of
the app than anything else left to convert: sixteen call sites across the
settings page, the plan form, the plan result, the margin tracker and the
reactions board. Four of the six `dbapi()` boundaries standing before this
commit pointed here.

The specific portability risk was the column name. `key` is on SQLAlchemy's
reserved-word list for SQLite and not for Postgres, so the *declaration*
renders as `"key"` on one and bare `key` on the other — one of only six tables
in the schema that compile differently at all. That is fine for reads and
writes (checked, not assumed: `key` is unreserved in Postgres), but it is the
kind of difference that would show up as a syntax error on the backend nobody
ran, which is precisely the failure this file exists to catch.

The other risk is duller and has bitten this project before: **SQLAlchemy opens
a transaction on first use and rolls it back on close**, where `sqlite3` in its
default isolation mode commits some statements for you. A `save_defaults` that
lost its `commit()` would still pass every same-connection assertion and lose
the write. `test_a_saved_default_survives_a_new_connection` is the one that
would notice.

Postgres comes from the container in `tests/test_postgres_schema.py`; without
it those parameterisations skip and the SQLite half still runs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.web import app_defaults as ad
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_app_defaults"


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    """An engine per backend, with the app tables present and empty.

    A file rather than `:memory:` on SQLite, because two of these tests open a
    *second* connection to check that a write actually committed — and every
    `:memory:` connection is a distinct, empty database that merely shares a
    name.
    """
    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'defaults.db'}")
        with eng.connect() as c:
            from app.db.schema import apply_schema
            apply_schema(c.connection.driver_connection)
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
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def _backend(conn) -> str:
    return conn.engine.dialect.name


# ── the control ──────────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing suite: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    conn.execute(text("SELECT 1"))


# ── the reserved-word column ─────────────────────────────────────────────────

def test_the_key_column_can_be_read_on_both_backends(conn):
    """`SELECT key, value FROM app_defaults` — `key` is quoted in the SQLite
    declaration and bare in the Postgres one. If it needed quoting to be read
    back, this is where that shows up, rather than on the settings page."""
    defaults = ad.get_defaults(conn)

    assert defaults["input_basis"] == "sell", f"on {_backend(conn)}: {defaults}"
    assert defaults["build_station_id"] == 0


def test_the_key_column_can_be_written_on_both_backends(conn):
    """The insert names the column too, in both the column list and the
    `ON CONFLICT` target."""
    saved = ad.save_defaults(conn, {"build_station_id": "60003760"})

    assert saved["build_station_id"] == 60003760, f"on {_backend(conn)}: {saved}"


# ── the transaction trap ─────────────────────────────────────────────────────

def test_a_saved_default_survives_a_new_connection(engine):
    """The one that catches a lost `commit()`.

    SQLAlchemy opens a transaction on first use and rolls it back when the
    connection closes; `sqlite3` in its default isolation mode commits some
    statements for itself. So a converted writer that dropped its commit passes
    every assertion made on the same connection and loses the write the moment
    the request ends. Asking a *different* connection is the only version of
    this question that can fail.
    """
    with engine.connect() as c:
        ad.save_defaults(c, {"facility_tax": "1.5", "build_station_id": "60003760"})

    with engine.connect() as c:
        reread = ad.get_defaults(c)

    assert reread["facility_tax"] == pytest.approx(1.5), (
        f"on {engine.dialect.name}: the write did not commit")
    assert reread["build_station_id"] == 60003760


def test_the_connection_is_usable_after_a_save(conn):
    """Postgres aborts the whole transaction on a failed statement and refuses
    every later one until rollback. A save that left the connection in that
    state would surface as a failure in whatever ran next, which on the
    settings page is the station list."""
    ad.save_defaults(conn, {"sell_venue": "upwell"})

    assert conn.execute(text("SELECT 1")).scalar() == 1


# ── the upsert ───────────────────────────────────────────────────────────────

def test_saving_a_key_twice_updates_rather_than_duplicating(conn):
    """`ON CONFLICT(key) DO UPDATE`. The table is read back as a dict keyed on
    `key`, so a duplicate row would not raise — it would just mean the value
    you see depends on row order, which is the worst way for this to fail."""
    ad.save_defaults(conn, {"facility_tax": "1.5"})
    ad.save_defaults(conn, {"facility_tax": "3.25"})

    rows = conn.execute(text(
        "SELECT value FROM app_defaults WHERE key = 'facility_tax'")).fetchall()

    assert len(rows) == 1, f"on {_backend(conn)}: {len(rows)} rows for one key"
    assert ad.get_defaults(conn)["facility_tax"] == pytest.approx(3.25)


def test_saving_one_key_leaves_the_others_alone(conn):
    """The settings page posts the whole form, but the plan form and the API
    both save subsets. `INSERT OR REPLACE` would have been fine here because
    the table is two columns — but `DO UPDATE` is what is written, and this
    pins that a partial save is partial."""
    ad.save_defaults(conn, {"facility_tax": "1.5", "industry_skill": "3"})
    ad.save_defaults(conn, {"facility_tax": "2.0"})

    out = ad.get_defaults(conn)

    assert out["facility_tax"] == pytest.approx(2.0)
    assert out["industry_skill"] == 3, "an unrelated key was reset"


# ── the schema shim ──────────────────────────────────────────────────────────

def test_the_schema_shim_does_not_reach_for_a_pragma_on_postgres(conn):
    """`app/db/schema.py` memoises per database by asking `PRAGMA
    database_list`, which is a syntax error on Postgres. The shim returns early
    there — the schema arrives through Alembic instead. Calling it is how a
    regression would announce itself."""
    ad.ensure_defaults_table(conn)
    ad.ensure_defaults_table(conn)          # twice: memoised on one, no-op on the other

    assert conn.execute(text("SELECT COUNT(*) FROM app_defaults")).scalar() == 0


# ── coercion, which is where a key/value table earns its keep ────────────────

def test_values_come_back_typed_not_as_strings(conn):
    """Everything is stored as text. `DEFAULTS` carries the coercer, and the
    callers index the result straight into arithmetic — `facility_tax` reaching
    a caller as `"1.5"` would concatenate rather than add."""
    ad.save_defaults(conn, {"facility_tax": "1.5", "industry_skill": "4",
                            "sell_venue": "upwell"})

    out = ad.get_defaults(conn)

    assert isinstance(out["facility_tax"], float), f"on {_backend(conn)}"
    assert isinstance(out["industry_skill"], int)
    assert isinstance(out["sell_venue"], str)


def test_an_unparseable_stored_value_falls_back_instead_of_raising(conn):
    """A hand-edited row must not take the page down. Written directly rather
    than through `save_defaults`, because that coerces on the way in — the row
    this guards against is one no writer of ours produced."""
    conn.execute(text("INSERT INTO app_defaults (key, value)"
                      " VALUES ('facility_tax', 'not-a-number')"))
    conn.commit()

    out = ad.get_defaults(conn)

    assert out["facility_tax"] == pytest.approx(2.5), (
        f"on {_backend(conn)}: a bad row was not replaced by the default")


def test_an_unknown_key_is_not_stored(conn):
    """The table is read back with `DEFAULTS` as its schema, so a row nothing
    knows how to coerce is dead weight — and on a key/value table it is the
    kind of dead weight nobody ever notices."""
    ad.save_defaults(conn, {"nonsense_key": "1", "facility_tax": "1.5"})

    stored = {r[0] for r in conn.execute(text("SELECT key FROM app_defaults"))}

    assert "nonsense_key" not in stored, f"on {_backend(conn)}: {stored}"
    assert "facility_tax" in stored, "the recognised key was dropped too"


def test_an_unset_key_reports_its_default_rather_than_being_absent(conn):
    """Callers index the dict directly — `defaults["max_job_days"]` — so a
    missing key is a KeyError on a page, not a None to check."""
    out = ad.get_defaults(conn)

    assert set(out) == set(ad.DEFAULTS), (
        f"on {_backend(conn)}: missing {sorted(set(ad.DEFAULTS) - set(out))}")


def test_is_configured_follows_the_stored_station(conn):
    """The one default with no sane fallback: without a station there is no
    system cost index, so a profit figure would be fiction."""
    assert ad.is_configured(ad.get_defaults(conn)) is False

    ad.save_defaults(conn, {"build_station_id": "60003760"})

    assert ad.is_configured(ad.get_defaults(conn)) is True
