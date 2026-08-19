"""`industry` is converted. This runs its helpers on both backends.

The lesson from `projects`: a conversion tested only on SQLite is a conversion
tested on the backend it was already working on. Mutating the qualified
`ON CONFLICT` there failed 7 Postgres tests and **zero** SQLite ones — that gap
is the whole point, and it only appears if the same assertion runs twice.

What this slice exposes that `projects` did not:

* **A driver-specific exception class.** `add_item` used to catch
  `sqlite3.IntegrityError` to turn a duplicate watchlist row into a friendly
  message. psycopg does not raise that, so on Postgres the duplicate would have
  escaped as a 500 — and it would have failed *silently* in the sense that
  nothing on SQLite could ever notice.
* **`PRAGMA table_info`.** `_volume_column_present` asked SQLite directly
  whether `sde_types.packaged_volume` exists. That pragma does not exist on
  Postgres. It is SQLAlchemy's inspector now, which asks the same question in
  whichever dialect is underneath.

Postgres comes from the container in `tests/test_postgres_schema.py`; without
it those parameterisations skip and the SQLite half still runs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.web import margins_helper as mh
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_industry"

#: A made-up buildable product. `add_item` checks the SDE for the name and for
#: a blueprint, so both backends need those two rows — on Postgres the SDE
#: tables exist (the schema declares them) but are empty.
FAKE_TYPE = 90_000_001


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'industry.db'}")
        with engine.connect() as c:
            from app.db.schema import apply_schema, apply_sde_schema
            raw = c.connection.driver_connection
            apply_schema(raw)
            apply_sde_schema(raw)
            _seed(c)
            yield c
        engine.dispose()
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

    engine = create_engine(scoped)

    # The migrations create APP_TABLES only — `test_postgres_schema.py` asserts
    # exactly that. The SDE tables are not in the Alembic history, because
    # `import_sde.py` builds them with `apply_sde_schema` against SQLite and has
    # no Postgres path yet. That is a real gap in the deployment story (six
    # statements JOIN `sde_types` to runtime tables, so Postgres cannot serve
    # the app without them) and it is recorded in the worklist. Here the fixture
    # creates them so the *conversion* can be tested independently of it.
    from app.db.schema import SDE_TABLES, metadata
    metadata.create_all(engine, tables=[metadata.tables[n] for n in sorted(SDE_TABLES)])

    with engine.connect() as c:
        _seed(c)
        yield c
    engine.dispose()


def _seed(c):
    c.execute(text("INSERT INTO sde_types (type_id, name, published)"
                   " VALUES (:tid, 'Test Widget', 1)"), {"tid": FAKE_TYPE})
    c.execute(text("INSERT INTO sde_blueprint_products"
                   " (blueprint_type_id, product_type_id, activity, quantity)"
                   " VALUES (:bp, :tid, 'manufacturing', 1)"),
              {"bp": FAKE_TYPE + 1, "tid": FAKE_TYPE})
    c.commit()


def _backend(conn) -> str:
    return conn.engine.dialect.name


def _row(margin_pct=25.0, profit=1000.0, sell_price=5000.0):
    from app.manufacturing.margins import MarginRow
    r = MarginRow(type_id=FAKE_TYPE, name="Test Widget", group_name="g", me=0, te=0)
    r.margin_pct, r.profit, r.sell_price = margin_pct, profit, sell_price
    return r


# ── the control ──────────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture looks like a passing suite: the
    SQLite half would carry it, and running on both is the entire point."""
    assert _backend(conn) in ("sqlite", "postgresql")
    conn.execute(text("SELECT 1"))


# ── the driver-specific exception ────────────────────────────────────────────

def test_a_duplicate_watchlist_entry_is_a_message_not_a_crash(conn):
    """The catch used to be `sqlite3.IntegrityError`, which psycopg never
    raises. On Postgres the duplicate would have escaped as a 500."""
    ok, _ = mh.add_item(conn, FAKE_TYPE, me=10, te=20)
    assert ok, "the first add should succeed"

    again, message = mh.add_item(conn, FAKE_TYPE, me=10, te=20)

    assert again is False, f"on {_backend(conn)}: a duplicate was accepted"
    assert "already tracked" in message, (
        f"on {_backend(conn)}: the duplicate did not produce the friendly "
        f"message — got {message!r}")


def test_the_connection_still_works_after_a_duplicate(conn):
    """Postgres aborts the transaction on a failed statement and refuses every
    later one until rollback. A catch that does not roll back leaves the whole
    request broken, with the damage surfacing in an unrelated query."""
    mh.add_item(conn, FAKE_TYPE, me=10, te=20)
    mh.add_item(conn, FAKE_TYPE, me=10, te=20)          # the failing one

    items = mh.list_items(conn)

    assert len(items) == 1, (
        f"on {_backend(conn)}: the connection did not survive the duplicate")


# ── the introspection that used to be a pragma ───────────────────────────────

def test_the_volume_column_probe_works_on_both_backends(conn):
    """`PRAGMA table_info` does not exist on Postgres. The inspector does."""
    assert mh._volume_column_present(conn) is True, (
        f"on {_backend(conn)}: sde_types.packaged_volume was not found")


def test_the_probe_is_answering_about_the_real_table(conn):
    """A probe that returns True unconditionally would pass the test above.
    Ask it about a column that certainly is not there."""
    from sqlalchemy import inspect
    cols = {c["name"] for c in inspect(conn).get_columns("sde_types")}
    assert "packaged_volume" in cols
    assert "definitely_not_a_column" not in cols


# ── the watchlist and its history ────────────────────────────────────────────

def test_a_watchlist_entry_round_trips(conn):
    ok, _ = mh.add_item(conn, FAKE_TYPE, me=10, te=20)
    assert ok

    items = mh.list_items(conn)
    assert len(items) == 1
    assert items[0]["type_id"] == FAKE_TYPE
    assert (items[0]["me"], items[0]["te"]) == (10, 20)


def test_an_unbuildable_product_is_refused(conn):
    """The SDE lookup has to work on both backends too, not just the insert."""
    ok, message = mh.add_item(conn, 12_345_678, me=0, te=0)
    assert ok is False
    assert "Unknown item" in message


def test_a_snapshot_updates_rather_than_duplicating(conn):
    """`ON CONFLICT (item_id, day) DO UPDATE` — today's row is rewritten on
    every page load, so a second write must replace, not append."""
    mh.add_item(conn, FAKE_TYPE, me=10, te=20)
    item_id = mh.list_items(conn)[0]["id"]

    mh.record_snapshot(conn, item_id, _row(margin_pct=25.0))
    mh.record_snapshot(conn, item_id, _row(margin_pct=31.5))
    conn.commit()

    rows = conn.execute(
        text("SELECT margin_pct FROM margin_snapshot WHERE item_id = :iid"),
        {"iid": item_id}).fetchall()

    assert len(rows) == 1, f"on {_backend(conn)}: {len(rows)} rows for one day"
    assert rows[0][0] == pytest.approx(31.5), "the later reading did not win"


def test_an_unpriced_row_is_not_recorded(conn):
    """A row that could not be priced is not a data point; averaging it in
    would drag the rolling margin toward a number nobody observed."""
    mh.add_item(conn, FAKE_TYPE, me=10, te=20)
    item_id = mh.list_items(conn)[0]["id"]

    mh.record_snapshot(conn, item_id, _row(margin_pct=None))
    conn.commit()

    count = conn.execute(
        text("SELECT COUNT(*) FROM margin_snapshot WHERE item_id = :iid"),
        {"iid": item_id}).scalar()
    assert count == 0


def test_removing_an_item_takes_its_history_with_it(conn):
    """Otherwise a re-added item inherits a stranger's past readings."""
    mh.add_item(conn, FAKE_TYPE, me=10, te=20)
    item_id = mh.list_items(conn)[0]["id"]
    mh.record_snapshot(conn, item_id, _row())
    conn.commit()

    mh.remove_item(conn, item_id)

    assert mh.list_items(conn) == []
    left = conn.execute(
        text("SELECT COUNT(*) FROM margin_snapshot WHERE item_id = :iid"),
        {"iid": item_id}).scalar()
    assert left == 0, f"on {_backend(conn)}: {left} orphaned snapshot rows"


def test_history_reads_back_what_was_written(conn):
    mh.add_item(conn, FAKE_TYPE, me=10, te=20)
    item_id = mh.list_items(conn)[0]["id"]
    mh.record_snapshot(conn, item_id, _row(margin_pct=42.0))
    conn.commit()

    hist = mh.history_for(conn, item_id)

    # Only today's row exists, and `prev` deliberately excludes today.
    assert hist["prev_margin"] is None
    assert hist["avg_margin"] == pytest.approx(42.0)
    assert hist["days"] == 1
