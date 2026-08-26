"""Margin tracker — profit engine, watchlist persistence and history.

Runs against the committed ``sde_base.db``. The engine is cache-only by design,
so these tests seed the price caches directly and never touch the network.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import tempfile

import pytest

from app.db.conn import connect_to_path, dbapi


def _db_file(conn) -> str:
    """The file a sqlite3 connection is attached to. `database_list` returns
    (seq, name, file) per attached database; `main` is the one we opened."""
    return [r[2] for r in conn.exec_driver_sql("PRAGMA database_list") if r[1] == "main"][0]

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDE = os.path.join(REPO, "sde_base.db")

CRANE = 12729           # Blockade Runner, a T2 ship with a deep tree
TRITANIUM = 34


@pytest.fixture
def db(tmp_path):
    """A throwaway copy of the SDE with the runtime tables created."""
    path = str(tmp_path / "eve_cache.db")
    shutil.copy2(SDE, path)
    conn = sqlite3.connect(path)
    from app.db.schema import ensure_schema
    # Two shims used to be called here and both were dropped for the same
    # reason, one conversion apart: `industry_helper.ensure_industry_tables`
    # and `market.prices.ensure_price_table`. Each only ever forwarded to
    # `ensure_schema` below, and each now takes a SQLAlchemy connection rather
    # than this DBAPI one.
    ensure_schema(conn)
    conn.close()

    # The module under test is on the portable query layer now, so the fixture
    # hands out a SQLAlchemy connection. Schema creation above stays on the
    # DBAPI because `app/db/schema.py` has not been converted yet — it is the
    # same file either way.
    engine_conn = connect_to_path(path)
    yield path, engine_conn
    engine_conn.close()


def _price(conn, type_id, sell, buy=None):
    conn.exec_driver_sql(
        "INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) "
        "VALUES (?,?,?,?)", (type_id, sell, buy if buy is not None else sell * 0.9, 0))
    conn.commit()


def _price_whole_tree(conn, path, type_id, me, unit_price=100.0):
    """Gives every BOM leaf a cached price so a row can price fully.

    Includes the datacores for a T2 product: since invention is modelled, an
    unpriced datacore is reported exactly like an unpriced material, and a row
    that cannot price its blueprint is not fully priced.
    """
    from app.bom.resolver import BOMResolver
    resolver = BOMResolver(connect_to_path(path))
    try:
        leaves = resolver.resolve(type_id, 1, me=me).aggregate_leaves()
    finally:
        resolver.close()
    for leaf_id in leaves:
        _price(conn, leaf_id, unit_price)
    for dc_id in _datacores(conn, type_id):
        _price(conn, dc_id, unit_price)
    return leaves


def _datacores(conn, type_id):
    """Datacore type ids consumed inventing `type_id`, empty if not invented.

    `invention` is on the portable query layer now, so it needs a SQLAlchemy
    connection rather than the raw sqlite3 one this fixture otherwise uses.
    Both open the same file.
    """
    from app.manufacturing.invention import find_recipe
    with connect_to_path(_db_file(conn)) as sde:
        recipe = find_recipe(sde, type_id)
    return [tid for tid, _name, _qty in recipe.datacores] if recipe else []


# ── the engine ───────────────────────────────────────────────────────────────
def test_profit_is_sell_minus_materials_and_fees(db):
    """The headline arithmetic, on prices we control end to end."""
    path, conn = db
    from app.manufacturing.margins import compute_margin
    from app.web.app_defaults import get_defaults

    leaves = _price_whole_tree(conn, path, CRANE, me=0, unit_price=10.0)
    _price(conn, CRANE, 500_000_000.0)

    row = compute_margin(conn, path, CRANE, me=0, te=0, defaults=get_defaults(conn))

    assert row.name == "Crane"
    assert row.group_name == "Blockade Runner"        # the Group column
    assert row.unpriced == []
    assert row.sell_price == 500_000_000.0
    assert row.material_cost > 0
    # profit = sell − materials − job fee − selling − inventing the blueprint.
    assert row.profit == pytest.approx(
        row.sell_price - row.material_cost - row.job_fee
        - row.selling_cost - row.invention_cost)
    # The Crane is T2, so it is charged for the BPC it came from.
    assert row.invention_cost > 0
    assert 0 < row.invention_chance <= 1
    # An unconfigured install assumes untrained skills and no standings, which
    # is the *pessimistic* end: 7.5% sales tax + 3% broker fee on a listed order.
    assert row.selling_cost_pct == pytest.approx(10.5)
    assert row.selling_cost == pytest.approx(row.sell_price * 0.105)
    assert row.margin_pct == pytest.approx(row.profit / row.sell_price * 100)
    assert row.build_seconds > 0
    assert row.profit_per_hour == pytest.approx(
        row.profit * row.units_per_run / (row.build_seconds / 3600))
    assert len(leaves) > 1


def test_unpriced_material_is_reported_not_treated_as_free(db):
    """The safety property this whole design turns on.

    A material with no cached price must never silently count as zero — that
    understates cost and overstates profit, which is exactly the error that
    makes a margin tracker worse than useless.
    """
    path, conn = db
    from app.manufacturing.margins import compute_margin
    from app.web.app_defaults import get_defaults

    leaves = _price_whole_tree(conn, path, CRANE, me=0, unit_price=10.0)
    _price(conn, CRANE, 500_000_000.0)
    priced_cost = compute_margin(conn, path, CRANE, 0, 0, get_defaults(conn)).material_cost

    # Drop one leaf's price and re-price.
    dropped = sorted(leaves)[0]
    conn.exec_driver_sql("DELETE FROM market_price_cache WHERE type_id=?", (dropped,))
    conn.commit()
    row = compute_margin(conn, path, CRANE, 0, 0, get_defaults(conn))

    assert row.unpriced, "a missing price must be reported"
    assert row.priced is False
    assert row.material_cost < priced_cost      # it really is missing from the total
    assert leaves[dropped][0] in row.unpriced   # …and named, so the UI can say which


def test_negative_me_costs_more(db):
    """ME can go negative, and that must raise material cost, not crash."""
    path, conn = db
    from app.manufacturing.margins import compute_margin
    from app.web.app_defaults import get_defaults

    _price_whole_tree(conn, path, CRANE, me=-4, unit_price=10.0)
    _price(conn, CRANE, 500_000_000.0)
    defaults = get_defaults(conn)

    worse = compute_margin(conn, path, CRANE, me=-4, te=0, defaults=defaults)
    better = compute_margin(conn, path, CRANE, me=10, te=0, defaults=defaults)

    assert worse.material_cost > better.material_cost
    assert worse.profit < better.profit


def test_unbuildable_product_reports_an_error(db):
    """Tritanium has no blueprint — there is no margin to compute."""
    path, conn = db
    from app.manufacturing.margins import compute_margin
    from app.web.app_defaults import get_defaults

    row = compute_margin(conn, path, TRITANIUM, 0, 0, get_defaults(conn))
    assert row.error is not None
    assert row.profit is None


def test_day_volume_prefers_history_over_the_weekly_total(db):
    """Daily volume comes from the cached history series when present."""
    path, conn = db
    import json
    from app.manufacturing.margins import _avg_day_volume, JITA_REGION

    conn.exec_driver_sql(
        "INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, volume, cached_at) "
        "VALUES (?,?,?,?)", (CRANE, 1.0, 700, 0))
    conn.commit()
    assert _avg_day_volume(conn, CRANE) == pytest.approx(100.0)   # 700 / 7

    series = [{"date": "2026-08-0%d" % d, "volume": 40} for d in range(1, 8)]
    conn.exec_driver_sql(
        "INSERT OR REPLACE INTO price_history_cache (region_id, type_id, data_json, cached_at) "
        "VALUES (?,?,?,?)", (JITA_REGION, CRANE, json.dumps(series), 0))
    conn.commit()
    assert _avg_day_volume(conn, CRANE) == pytest.approx(40.0)


# ── watchlist ────────────────────────────────────────────────────────────────
def test_watchlist_add_reject_and_remove(db):
    path, conn = db
    from app.web import margins_helper as mh

    ok, msg = mh.add_item(conn, CRANE, 5, 20)
    assert ok and "Crane" in msg

    ok, msg = mh.add_item(conn, CRANE, 5, 20)
    assert not ok and "already tracked" in msg

    # Same product at a different ME is a genuinely different proposition.
    ok, _ = mh.add_item(conn, CRANE, 10, 20)
    assert ok
    assert len(mh.list_items(conn)) == 2

    ok, msg = mh.add_item(conn, TRITANIUM, 0, 0)
    assert not ok and "no blueprint" in msg

    mh.remove_item(conn, mh.list_items(conn)[0]["id"])
    assert len(mh.list_items(conn)) == 1
    mh.clear_all(conn)
    assert mh.list_items(conn) == []


def test_removing_an_item_drops_its_history(db):
    """Otherwise a re-added item would inherit a stranger's past readings."""
    path, conn = db
    from app.web import margins_helper as mh

    mh.add_item(conn, CRANE, 5, 20)
    item_id = mh.list_items(conn)[0]["id"]
    conn.exec_driver_sql("INSERT INTO margin_snapshot (item_id, day, margin_pct) VALUES (?,?,?)",
                 (item_id, "2026-08-01", 12.5))
    conn.commit()

    mh.remove_item(conn, item_id)
    left = conn.exec_driver_sql("SELECT COUNT(*) FROM margin_snapshot WHERE item_id=?",
                        (item_id,)).fetchone()[0]
    assert left == 0


# ── history ──────────────────────────────────────────────────────────────────
def _seed_days(conn, item_id, margins_by_offset):
    today = dt.datetime.now(dt.timezone.utc).date()
    for offset, margin in margins_by_offset.items():
        day = (today - dt.timedelta(days=offset)).strftime("%Y-%m-%d")
        conn.exec_driver_sql(
            "INSERT OR REPLACE INTO margin_snapshot (item_id, day, margin_pct) VALUES (?,?,?)",
            (item_id, day, margin))
    conn.commit()


def test_change_compares_against_the_previous_day_not_today(db):
    """Today's row is rewritten on every page load, so the delta has to look
    back past it — otherwise it would always read zero."""
    path, conn = db
    from app.web.margins_helper import history_for

    _seed_days(conn, 1, {0: 20.0, 1: 12.0, 2: 5.0})
    hist = history_for(conn, 1)
    assert hist["prev_margin"] == 12.0          # yesterday, not today's 20.0


def test_rolling_average_and_partial_window(db):
    path, conn = db
    from app.web.margins_helper import AVG_WINDOW_DAYS, history_for

    _seed_days(conn, 1, {0: 10.0, 1: 20.0, 2: 30.0})
    hist = history_for(conn, 1)
    assert hist["avg_margin"] == pytest.approx(20.0)
    assert hist["days"] == 3
    assert hist["full_window"] is False         # three readings is not a week

    _seed_days(conn, 1, {d: 10.0 for d in range(3, AVG_WINDOW_DAYS + 3)})
    hist = history_for(conn, 1)
    assert hist["days"] == AVG_WINDOW_DAYS
    assert hist["full_window"] is True
    # Only the newest AVG_WINDOW_DAYS readings count.
    assert hist["avg_margin"] == pytest.approx((10.0 + 20.0 + 30.0 + 10.0 * 4) / 7)


def test_unpriced_rows_are_not_recorded_as_history(db):
    """A row we could not price is not a data point — averaging it in would
    drag the trend toward a number that was never true."""
    path, conn = db
    from app.manufacturing.margins import MarginRow
    from app.web.margins_helper import record_snapshot

    record_snapshot(conn, 1, MarginRow(type_id=CRANE, name="Crane", group_name="x",
                                       me=0, te=0, margin_pct=None))
    assert conn.exec_driver_sql("SELECT COUNT(*) FROM margin_snapshot").fetchone()[0] == 0


# ── defaults ─────────────────────────────────────────────────────────────────
def test_defaults_round_trip_and_survive_garbage(db):
    path, conn = db
    from app.web.app_defaults import get_defaults, is_configured, save_defaults

    base = get_defaults(conn)
    assert base["build_station_id"] == 0
    assert is_configured(base) is False

    saved = save_defaults(conn, {"build_station_id": "60003760", "facility_tax": "1.5",
                                 "input_basis": "buy", "not_a_key": "ignored"})
    assert saved["build_station_id"] == 60003760
    assert saved["facility_tax"] == 1.5
    assert saved["input_basis"] == "buy"
    assert "not_a_key" not in saved
    assert is_configured(saved) is True

    # A hand-mangled row must fall back, not take the page down.
    conn.exec_driver_sql("UPDATE app_defaults SET value='banana' WHERE key='facility_tax'")
    conn.commit()
    assert get_defaults(conn)["facility_tax"] == 2.5


# ── routes ───────────────────────────────────────────────────────────────────
def test_margins_page_renders_and_tracks(client):
    from urllib.parse import unquote

    assert client.get("/margins").status_code == 200
    r = client.post("/margins/add", data={"product": "Crane", "me": "5", "te": "20"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Added Crane" in unquote(r.headers["location"])

    page = client.get("/margins").text
    assert "Crane" in page and "Blockade Runner" in page
    # No prices cached in the test fixture, so the row must say so rather than
    # showing a confident profit.
    assert "unpriced" in page

    client.post("/margins/clear", follow_redirects=False)
    assert "Nothing tracked yet" in client.get("/margins").text


def test_settings_exposes_industry_defaults(client):
    page = client.get("/settings").text
    assert "Industry defaults" in page
    assert client.post("/api/settings/defaults",
                       json={"build_station_id": "60003760"}).json()["ok"] is True


# ── The acquisition broker fee ───────────────────────────────────────────────
#
# `app/market/taxes.py` was sell-side only: buying materials was modelled as
# free on every basis. It is free on one — buying instantly off a sell order.
# Placing your own buy order costs a broker fee, and the input-basis setting
# picks between exactly those two. Understated cost is overstated profit, so
# this failed in the flattering direction.

def _margin_on(conn, path, basis, **extra):
    from app.manufacturing.margins import compute_margin
    from app.web.app_defaults import get_defaults

    defaults = dict(get_defaults(conn))
    defaults["input_basis"] = basis
    defaults.update(extra)
    return compute_margin(conn, path, CRANE, me=0, te=0, defaults=defaults)


def _price_tree_flat(conn, path, unit=10.0):
    """Every leaf at the same price on *both* sides.

    The shared fixture prices buy at 90% of sell, which is realistic and useless
    here: comparing bases would then measure the spread, not the fee. With both
    sides equal the only thing that can move the cost is the broker fee.
    """
    from app.bom.resolver import BOMResolver
    resolver = BOMResolver(connect_to_path(path))
    leaves = resolver.resolve(CRANE, 1, me=0).aggregate_leaves()
    for leaf_id in leaves:
        _price(conn, leaf_id, unit, buy=unit)
    for dc_id in _datacores(conn, CRANE):
        _price(conn, dc_id, unit, buy=unit)
    _price(conn, CRANE, 500_000_000.0)
    return leaves


def test_buying_on_orders_costs_more_than_buying_instantly(db):
    """The whole point, stated as an event: at the same quoted price, the basis
    that requires placing an order has to cost more than taking one."""
    path, conn = db
    _price_tree_flat(conn, path)

    instant = _margin_on(conn, path, "sell", broker_relations_skill=0)
    on_orders = _margin_on(conn, path, "buy", broker_relations_skill=0)

    assert on_orders.material_cost > instant.material_cost, (
        "placing buy orders was costed as free — the broker fee is missing")


def test_the_markup_is_exactly_the_broker_rate(db):
    """Not just "more": the right amount more. A fee applied twice, or applied
    to the job fee as well, would also pass the test above."""
    path, conn = db
    from app.market.taxes import buying_costs
    _price_tree_flat(conn, path)

    instant = _margin_on(conn, path, "sell", broker_relations_skill=0)
    on_orders = _margin_on(conn, path, "buy", broker_relations_skill=0)
    rate = buying_costs("buy", {"broker_relations_skill": 0}).broker_fee

    assert on_orders.material_cost == pytest.approx(
        instant.material_cost * (1.0 + rate)), (
        f"expected exactly a {rate:.1%} markup on the material cost")


def test_broker_relations_shows_up_in_the_build_cost(db):
    """The skill has to reach the number, not just the fee helper."""
    path, conn = db
    _price_tree_flat(conn, path)

    untrained = _margin_on(conn, path, "buy", broker_relations_skill=0)
    trained = _margin_on(conn, path, "buy", broker_relations_skill=5)

    assert trained.material_cost < untrained.material_cost, (
        "Broker Relations does not reach the material cost")


def test_a_custom_override_is_not_marked_up(db):
    """An override is a deliberate "this is what it really costs me". Adding a
    broker fee on top contradicts the statement the user made."""
    path, conn = db
    from app.manufacturing.margins import _cached_prices

    leaves = _price_tree_flat(conn, path)
    target = next(iter(leaves))
    conn.exec_driver_sql("INSERT OR REPLACE INTO custom_price_override (type_id, price)"
                 " VALUES (?,?)", (target, 10.0))
    conn.commit()

    prices, overridden = _cached_prices(conn, {target})
    assert target in overridden, "the override was not reported as one"

    with_override = _margin_on(conn, path, "buy", broker_relations_skill=0)
    conn.exec_driver_sql("DELETE FROM custom_price_override WHERE type_id=?", (target,))
    conn.commit()
    without = _margin_on(conn, path, "buy", broker_relations_skill=0)

    assert with_override.material_cost < without.material_cost, (
        "the overridden material was marked up like a market buy, so the "
        "user's stated real cost was overridden in turn")
