"""Reactions board — the whole reaction space, priced on direct inputs.

Runs against the committed ``sde_base.db`` with prices seeded directly, so the
arithmetic is checkable end to end and nothing touches the network.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

import pytest

from app.db.conn import connect_to_path, dbapi

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDE = os.path.join(REPO, "sde_base.db")

TUNGSTEN_CARBIDE = 16672        # two reaction blueprints in the SDE — see below
NITROGEN_FUEL_BLOCK = 4051


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "eve_cache.db")
    shutil.copy2(SDE, path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    from app.market.prices import ensure_price_table
    from app.web.industry_helper import ensure_industry_tables
    from app.web.location_resolver import ensure_location_name_table
    ensure_price_table(conn)
    ensure_industry_tables(conn)
    # Needed as soon as a station is configured: the station context resolves
    # the system behind it to read its cost index.
    ensure_location_name_table(conn)
    conn.close()

    # The module under test is on the portable query layer now, so the fixture
    # hands out a SQLAlchemy connection. Schema creation above stays on the
    # DBAPI because `app/db/schema.py` has not been converted yet — it is the
    # same file either way.
    engine_conn = connect_to_path(path)

    # A station is the one default with no sane fallback; without it the board
    # refuses to show numbers at all. Seeded *after* the engine connection
    # exists, because `app_defaults` is converted now and no longer takes the
    # raw handle.
    from app.web.app_defaults import save_defaults
    save_defaults(engine_conn,
                  {"build_station_id": 60003760, "reaction_station_id": 60003760})

    yield path, engine_conn
    engine_conn.close()


def _price(conn, type_id, sell, buy=None):
    conn.exec_driver_sql(
        "INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) "
        "VALUES (?,?,?,?)", (type_id, sell, buy if buy is not None else sell * 0.9, 0))
    conn.commit()


# ── the product set ──────────────────────────────────────────────────────────

def test_the_board_covers_every_published_reaction_exactly_once(db):
    """119, not the 111 in the design doc — §9.1 omits the 8 alchemy products.

    'Exactly once' is the load-bearing half. Tungsten Carbide has TWO reaction
    blueprints in the SDE, so a naive product+blueprint query returns 120 rows
    with one product listed twice.
    """
    _path, conn = db
    from app.web.reactions_helper import list_reaction_products

    products = list_reaction_products(conn)
    ids = [p["type_id"] for p in products]
    assert len(ids) == len(set(ids)), "a product is listed more than once"
    assert len(products) == 119

    groups = {p["group_name"] for p in products}
    assert "Unrefined Mineral" in groups, "the alchemy group is missing"


def test_tungsten_carbide_uses_the_real_formula_not_the_test_blueprint(db):
    """The SDE ships a 'Test Reaction Blueprint' yielding 20 beside the real
    formula yielding 10,000 — 500x apart, with a 360 s job against 10,800 s.
    Picking the wrong one silently misprices the row and its slot-hours."""
    _path, conn = db
    from app.bom.resolver import BOMResolver

    resolver = BOMResolver(connect_to_path(_path))
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
    finally:
        resolver.close()
    assert bp["product_qty"] == 10000
    assert bp["reaction_time"] == 10800


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_inputs_are_priced_directly_not_resolved_to_raw(db):
    """The reason the board does not reuse compute_margin.

    A Tungsten Carbide run consumes 5 Nitrogen Fuel Blocks; a fuel block job
    yields 40, and the resolver correctly refuses to run 1/8 of a job — so a
    recursive resolve charges a whole 40-block run, three times over. Costing
    the direct inputs is what running a reaction actually costs.

    Priced here with ONLY the direct inputs given a price: if the board were
    resolving to raw, those prices would be ignored and the cost would come
    from the (unpriced) raw leaves instead.
    """
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.reactions_helper import build_board

    resolver = BOMResolver(connect_to_path(path))
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
        mats = resolver.get_materials(bp["blueprint_type_id"], "reaction")
    finally:
        resolver.close()

    for mat in mats:
        _price(conn, mat["material_type_id"], 100.0)
    _price(conn, TUNGSTEN_CARBIDE, 1000.0)

    board = build_board(conn, path)
    row = next(r for r in board["rows"] if r["type_id"] == TUNGSTEN_CARBIDE)

    assert not row["unpriced"], f"unpriced despite every input priced: {row['unpriced']}"
    # Every direct input priced at 100 -> cost is 100 x the (ME-adjusted) total
    # quantity, which is bounded by the base quantities. A resolve-to-raw would
    # blow far past this, since it would be buying fuel-block ingredients.
    base_total = sum(m["quantity"] for m in mats)
    assert 0 < row["material_cost"] <= base_total * 100.0


def test_profit_is_revenue_minus_inputs_fee_and_selling_costs(db):
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.reactions_helper import build_board

    resolver = BOMResolver(connect_to_path(path))
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
        mats = resolver.get_materials(bp["blueprint_type_id"], "reaction")
    finally:
        resolver.close()
    for mat in mats:
        _price(conn, mat["material_type_id"], 10.0)
    _price(conn, TUNGSTEN_CARBIDE, 500.0)

    row = next(r for r in build_board(conn, path)["rows"]
               if r["type_id"] == TUNGSTEN_CARBIDE)
    revenue = row["sell_price"] * row["per_run"]
    expected = revenue - row["material_cost"] - row["job_fee"] - row["selling_cost"]
    assert row["per_job_profit"] == pytest.approx(expected)
    assert row["margin_pct"] == pytest.approx(expected / revenue * 100)


def test_jobs_per_period_is_floored(db):
    """A job that takes 5 days fits once into 7, not 1.4 times."""
    path, conn = db
    from app.web.reactions_helper import build_board, PERIOD_SECONDS

    for row in build_board(conn, path)["rows"]:
        if row["job_seconds"]:
            assert row["jobs_per_month"] == PERIOD_SECONDS // row["job_seconds"]
            assert isinstance(row["jobs_per_month"], int)


def test_slot_hours_use_the_reaction_job_not_the_whole_tree(db):
    """`MarginRow.build_seconds` covers the tree resolved to raw, which for a
    composite is far longer than the reaction itself. Ranking slot-hours on
    that would push every composite down the board for the wrong reason."""
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.reactions_helper import build_board

    resolver = BOMResolver(connect_to_path(path))
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
    finally:
        resolver.close()

    row = next(r for r in build_board(conn, path)["rows"]
               if r["type_id"] == TUNGSTEN_CARBIDE)
    # Facility bonuses only shorten it, and never below zero.
    assert 0 < row["job_seconds"] <= bp["reaction_time"]


# ── presentation ─────────────────────────────────────────────────────────────

def test_unprofitable_rows_are_kept_not_hidden(db):
    """The half a watchlist structurally cannot provide."""
    path, conn = db
    from app.web.reactions_helper import build_board

    # Nothing has an output price, so nothing can look good.
    board = build_board(conn, path)
    assert board["counts"]["total"] == 119
    assert board["counts"]["total"] == len(board["rows"])


def test_unknown_values_sort_last_rather_than_best(db):
    """A row with no price has no profit, and None must not top a descending
    ranking — it is not the best answer, it is the absence of one."""
    path, conn = db
    from app.web.reactions_helper import build_board

    _price(conn, TUNGSTEN_CARBIDE, 1_000_000.0)
    rows = build_board(conn, path, sort="slot_hour")["rows"]
    known = [i for i, r in enumerate(rows) if r["isk_per_slot_hour"] is not None]
    unknown = [i for i, r in enumerate(rows) if r["isk_per_slot_hour"] is None]
    if known and unknown:
        assert max(known) < min(unknown)


def test_alchemy_rows_are_flagged(db):
    """Their output is meant to be reprocessed, so the sell price priced here is
    not why the reaction is run. Flagged rather than dropped — silently removing
    eight rows would recreate the blind spot the board exists to remove."""
    path, conn = db
    from app.web.reactions_helper import build_board, ALCHEMY_GROUP

    rows = build_board(conn, path)["rows"]
    alchemy = [r for r in rows if r["is_alchemy"]]
    assert len(alchemy) == 8
    assert all(r["group_name"] == ALCHEMY_GROUP for r in alchemy)


def test_a_group_filter_narrows_rows_but_not_the_counts(db):
    """The summary describes the whole space; the filter is presentation."""
    path, conn = db
    from app.web.reactions_helper import build_board, ALCHEMY_GROUP

    board = build_board(conn, path, group=ALCHEMY_GROUP)
    assert len(board["rows"]) == 8
    assert board["counts"]["total"] == 119


def test_no_station_configured_shows_no_numbers(db):
    """Without a system cost index every profit figure would be fiction."""
    path, conn = db
    from app.web.app_defaults import save_defaults
    from app.web.reactions_helper import build_board

    save_defaults(conn, {"build_station_id": ""})
    board = build_board(conn, path)
    assert board["configured"] is False
    assert board["rows"] == []


# ── the page ─────────────────────────────────────────────────────────────────

def test_build_board_works_without_a_row_factory(db):
    """The real request path supplies a plain connection.

    `get_conn()` leaves the default row factory in place, while these fixtures
    set `sqlite3.Row` — so keyed row access passes every test here and raises on
    the live page. It did: this test exists because `test_the_page_renders`
    below went green against a board that returned at the not-configured guard
    and never ran a query at all.
    """
    path, conn = db
    conn.row_factory = None
    board = build_board_or_raise(conn, path)
    assert board["counts"]["total"] == 119


def build_board_or_raise(conn, path):
    from app.web.reactions_helper import build_board
    return build_board(conn, path)


def test_the_page_renders(client):
    """The real route. Note this renders the not-configured branch unless the
    app fixture has a build station, so it proves the template parses and the
    route is wired — not that the board priced anything."""
    r = client.get("/reactions")
    assert r.status_code == 200
    assert "Reactions Board" in r.text


def test_the_page_accepts_a_sort_and_a_group(client):
    from app.web.reactions_helper import ALCHEMY_GROUP

    r = client.get(f"/reactions?sort=margin&dir=asc&group={ALCHEMY_GROUP}")
    assert r.status_code == 200


def test_an_unknown_sort_falls_back_rather_than_erroring(client):
    """A hand-edited query string must not 500 the page."""
    assert client.get("/reactions?sort=nonsense").status_code == 200
    assert client.get("/reactions?sort=margin&dir=sideways").status_code == 200


def test_every_column_is_actually_sortable(db):
    """The headers are rendered from COLUMNS, so a key with no sort behind it
    would render a link that silently falls back to the default — which looks
    exactly like the click doing nothing."""
    path, conn = db
    from app.web.reactions_helper import build_board, COLUMNS

    for key, _heading, _align in COLUMNS:
        for direction in ("asc", "desc"):
            board = build_board(conn, path, sort=key, direction=direction)
            assert board["sort"] == key, f"{key} was not accepted as a sort"
            assert board["dir"] == direction
            assert len(board["rows"]) == 119


def test_ascending_does_not_promote_unreliable_rows(db):
    """Reliability is a separate pass precisely so reversing direction cannot
    hand the top of the board back to a row that cannot be sold."""
    path, conn = db
    from app.web.reactions_helper import build_board

    _price(conn, TUNGSTEN_CARBIDE, 10_000.0, buy=1.0)   # priced, but no real bid
    for direction in ("asc", "desc"):
        rows = build_board(conn, path, sort="margin", direction=direction)["rows"]
        reliable = [i for i, r in enumerate(rows) if r["reliable"]]
        unreliable = [i for i, r in enumerate(rows) if not r["reliable"]]
        if reliable and unreliable:
            assert max(reliable) < min(unreliable), direction


def test_unknown_values_stay_last_in_both_directions(db):
    """None is the absence of a value, not the smallest one — floating it to the
    top of an ascending sort is the same bug wearing a different hat."""
    path, conn = db
    from app.web.reactions_helper import build_board

    _price(conn, TUNGSTEN_CARBIDE, 5_000.0, buy=4_500.0)
    for direction in ("asc", "desc"):
        rows = build_board(conn, path, sort="slot_hour", direction=direction)["rows"]
        known = [i for i, r in enumerate(rows) if r["isk_per_slot_hour"] is not None]
        unknown = [i for i, r in enumerate(rows) if r["isk_per_slot_hour"] is None]
        if known and unknown:
            assert max(known) < min(unknown), direction


def test_sorting_actually_reverses(db):
    path, conn = db
    from app.web.reactions_helper import build_board

    def names(direction):
        rows = build_board(conn, path, sort="name", direction=direction)["rows"]
        return [r["name"] for r in rows]

    assert names("asc") == list(reversed(names("desc")))


# ── the ranking is a recommendation, so it has to be earned ─────────────────

def test_a_row_with_an_unpriced_input_cannot_top_the_board(db):
    """An unpriced input is costed at zero, which only ever flatters. Live, the
    board's #1 row combined that with a thin market and recommended reacting
    something that could not be sold."""
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.reactions_helper import build_board

    resolver = BOMResolver(connect_to_path(path))
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
        mats = resolver.get_materials(bp["blueprint_type_id"], "reaction")
    finally:
        resolver.close()

    # Tungsten Carbide: huge output price, but one input left unpriced.
    for mat in list(mats)[1:]:
        _price(conn, mat["material_type_id"], 1.0)
    _price(conn, TUNGSTEN_CARBIDE, 10_000.0)

    rows = build_board(conn, path, sort="margin")["rows"]
    tc_index = next(i for i, r in enumerate(rows) if r["type_id"] == TUNGSTEN_CARBIDE)
    reliable = [i for i, r in enumerate(rows) if r["reliable"]]

    assert rows[tc_index]["unpriced"], "expected the seeded input to be missing"
    assert not rows[tc_index]["reliable"]
    if reliable:
        assert tc_index > max(reliable), "an optimistic row outranked every real one"


def test_a_thin_market_is_flagged_and_demoted(db):
    """Sell 5.0M against a buy of 34.7k was live: one stale order, not a price
    you could realise."""
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.reactions_helper import build_board, THIN_MARKET_RATIO

    resolver = BOMResolver(connect_to_path(path))
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
        mats = resolver.get_materials(bp["blueprint_type_id"], "reaction")
    finally:
        resolver.close()
    for mat in mats:
        _price(conn, mat["material_type_id"], 1.0)

    # Everything priced, but the sell is far above any real bid.
    _price(conn, TUNGSTEN_CARBIDE, 10_000.0, buy=10_000.0 / (THIN_MARKET_RATIO * 2))
    row = next(r for r in build_board(conn, path)["rows"]
               if r["type_id"] == TUNGSTEN_CARBIDE)
    assert row["thin_market"] is True
    assert row["reliable"] is False

    # A normal spread on the same item is not flagged.
    _price(conn, TUNGSTEN_CARBIDE, 10_000.0, buy=9_000.0)
    row = next(r for r in build_board(conn, path)["rows"]
               if r["type_id"] == TUNGSTEN_CARBIDE)
    assert row["thin_market"] is False
    assert row["reliable"] is True


def test_demoted_rows_are_kept_with_their_numbers(db):
    """Demoted, not hidden — "looks great but nobody trades it" is worth
    knowing, and dropping the row would hide it."""
    path, conn = db
    from app.web.reactions_helper import build_board

    board = build_board(conn, path)
    assert board["counts"]["total"] == 119
    assert len(board["rows"]) == 119


# ── build-from-raw: whole units, amortised sub-runs ─────────────────────────

def test_raw_cost_amortises_a_sub_run_instead_of_charging_a_whole_job(db):
    """Needing 5 fuel blocks does not mean building 40.

    The rule: quantities of an intermediate are whole (4.8 blocks means 5), but
    the cost of one is a run's materials divided by its yield. Charging a whole
    40-block run is right when planning an actual build and wrong for a rate —
    it read -247% on Tungsten Carbide against +14% for buying the same inputs.
    """
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.app_defaults import get_defaults
    from app.manufacturing.margins import _station_context
    from app.web.reactions_helper import raw_unit_cost, _prices

    resolver = BOMResolver(connect_to_path(path))
    try:
        block_bp = resolver.find_blueprint(NITROGEN_FUEL_BLOCK)
        assert block_bp is not None and block_bp["product_qty"] > 1, \
            "fixture assumes a fuel block job yields many blocks"
        mats = resolver.get_materials(block_bp["blueprint_type_id"], "manufacturing")
        for mat in mats:
            _price(conn, mat["material_type_id"], 100.0)

        ctx = _station_context(conn, get_defaults(conn))
        prices = _prices(conn, {m["material_type_id"] for m in mats}, "buy")
        per_unit = raw_unit_cost(resolver, NITROGEN_FUEL_BLOCK, prices, ctx, {}, set())

        run_materials = sum(
            resolver._apply_me(m["quantity"], 1, 0.0, 1.0, runs_per_job=None) * 100.0
            for m in mats)
        yield_per_run = block_bp["product_qty"]
    finally:
        resolver.close()

    assert per_unit is not None
    assert per_unit < run_materials, "charged a whole run for one unit"
    assert per_unit == pytest.approx(run_materials / yield_per_run, rel=0.5)


def test_raw_cost_includes_manufacturing_job_fees_not_just_reaction_ones(db):
    """Install fees are ~12% of a composite's build cost, and a chain that
    manufactures its own fuel blocks pays the MANUFACTURING rate on those —
    a different rate from the reaction one (0.0897 vs 0.0861 live)."""
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.app_defaults import get_defaults
    from app.manufacturing.margins import _station_context
    from app.web.industry_helper import get_adjusted_prices_cached
    from app.web.reactions_helper import raw_unit_cost, _prices

    seed = BOMResolver(connect_to_path(path))
    try:
        for leaf_id in seed.resolve(TUNGSTEN_CARBIDE, 1).aggregate_leaves():
            _price(conn, leaf_id, 100.0)
    finally:
        seed.close()

    # The install fee is EIV x rate, and EIV comes from CCP's ADJUSTED prices —
    # a separate cache from the market one. Empty, every fee is zero whatever
    # the rate, which is what made the first version of this test vacuous.
    conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS adjusted_price_cache "
                 "(type_id INTEGER PRIMARY KEY, adjusted REAL, cached_at REAL)")
    for tid, in conn.exec_driver_sql("SELECT type_id FROM market_price_cache"):
        conn.exec_driver_sql("INSERT OR REPLACE INTO adjusted_price_cache VALUES (?,?,?)",
                     (tid, 100.0, 0))
    conn.commit()

    base_ctx = _station_context(conn, get_defaults(conn))
    adjusted = get_adjusted_prices_cached(dbapi(conn))
    ids = {r[0] for r in conn.exec_driver_sql("SELECT type_id FROM market_price_cache")}
    prices = _prices(conn, ids, "buy")

    def cost(rate_mfg, rate_rxn):
        resolver = BOMResolver(connect_to_path(path), blueprints=[], runs_per_job=None,
                               adjusted_prices=adjusted,
                               rate_mfg=rate_mfg, rate_rxn=rate_rxn)
        try:
            ctx = dict(base_ctx, rate_mfg=rate_mfg, rate_rxn=rate_rxn)
            return raw_unit_cost(resolver, TUNGSTEN_CARBIDE, prices, ctx, {}, set())
        finally:
            resolver.close()

    free = cost(0.0, 0.0)
    rxn_only = cost(0.0, 0.05)
    both = cost(0.05, 0.05)
    assert None not in (free, rxn_only, both)
    assert rxn_only > free, "reaction install fees are not charged"
    assert both > rxn_only, "manufacturing install fees are not charged"


def test_raw_cost_is_none_when_something_underneath_is_unpriced(db):
    """A partial raw cost is lower than the truth, and a Build Advantage built
    on it would favour building for a reason that is only missing data."""
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.app_defaults import get_defaults
    from app.manufacturing.margins import _station_context
    from app.web.reactions_helper import raw_unit_cost

    resolver = BOMResolver(connect_to_path(path))
    try:
        ctx = _station_context(conn, get_defaults(conn))
        unpriced = set()
        value = raw_unit_cost(resolver, TUNGSTEN_CARBIDE, {}, ctx, {}, unpriced)
    finally:
        resolver.close()
    assert value is None
    assert unpriced, "should name what it could not price"


def test_build_advantage_is_the_delta_between_the_two_models(db):
    path, conn = db
    from app.web.reactions_helper import build_board

    for row in build_board(conn, path, group="Composite")["rows"]:
        if row["margin_raw"] is not None and row["margin_int"] is not None:
            assert row["build_advantage"] == pytest.approx(
                row["margin_raw"] - row["margin_int"])
        else:
            assert row["build_advantage"] is None


# ── layout, freight and venue ───────────────────────────────────────────────

def test_the_composite_group_gets_the_wide_layout(db):
    path, conn = db
    from app.web.reactions_helper import build_board, WIDE_COLUMNS

    wide = build_board(conn, path, group="Composite")
    assert wide["wide"] is True
    assert wide["columns"] == WIDE_COLUMNS
    assert build_board(conn, path)["wide"] is False


def test_groups_without_intermediates_keep_the_narrow_layout(db):
    """An Intermediate Material has nothing to buy-or-build, so an int/raw pair
    would be the same number twice and imply a choice that does not exist."""
    path, conn = db
    from app.web.reactions_helper import build_board

    assert build_board(conn, path, group="Intermediate Materials")["wide"] is False


def test_export_is_output_volume_times_the_configured_rate(db):
    """Verified against the sheet: a consistent ISK per m3 across three products."""
    path, conn = db
    from app.web.app_defaults import save_defaults
    from app.web.reactions_helper import build_board

    save_defaults(conn, {"freight_export_isk_m3": 1000.0})
    _price(conn, TUNGSTEN_CARBIDE, 500.0)
    row = next(r for r in build_board(conn, path, group="Composite")["rows"]
               if r["type_id"] == TUNGSTEN_CARBIDE)
    vol = conn.exec_driver_sql("SELECT packaged_volume FROM sde_types WHERE type_id=?",
                       (TUNGSTEN_CARBIDE,)).fetchone()[0]
    units = row["per_run"] * row["jobs_per_month"]
    assert row["export"] == pytest.approx(units * vol * 1000.0)


def test_no_freight_configured_means_no_export_cost(db):
    """Correct for anyone building and selling in the same station."""
    path, conn = db
    from app.web.reactions_helper import build_board

    for row in build_board(conn, path, group="Composite")["rows"]:
        assert not row["export"]


def test_selling_in_jita_has_no_advantage_to_report(db):
    """Not zero. "No advantage" and "you are already there" are different
    statements, and a 0.0 in the column asserts the first."""
    path, conn = db
    from app.web.reactions_helper import build_board

    board = build_board(conn, path, group="Composite")
    assert board["venue"]["kind"] == "jita"
    assert all(r["sell_advantage"] is None for r in board["rows"])


def test_an_unfetched_hub_reports_nothing_rather_than_no_advantage(db):
    """hub_price_cache is empty until someone fetches that hub on /prices, and
    an empty cache must not read as "the prices happen to be identical"."""
    path, conn = db
    from app.web.app_defaults import save_defaults
    from app.web.reactions_helper import build_board

    save_defaults(conn, {"sell_hub_region_id": 10000042})    # Hek
    board = build_board(conn, path, group="Composite")
    assert board["venue"]["name"] == "Hek"
    assert board["venue"]["fetched"] is False
    assert all(r["sell_advantage"] is None for r in board["rows"])


def test_dumping_to_buy_orders_prices_the_output_at_the_buy_price(db):
    """sales_method "immediate" used to drop the broker fee while still valuing
    output at the SELL price, overstating profit twice over."""
    path, conn = db
    from app.web.app_defaults import save_defaults
    from app.web.reactions_helper import build_board

    _price(conn, TUNGSTEN_CARBIDE, 1000.0, buy=400.0)

    save_defaults(conn, {"sales_method": "orders"})
    listed = next(r for r in build_board(conn, path)["rows"]
                  if r["type_id"] == TUNGSTEN_CARBIDE)
    save_defaults(conn, {"sales_method": "immediate"})
    dumped = next(r for r in build_board(conn, path)["rows"]
                  if r["type_id"] == TUNGSTEN_CARBIDE)

    assert listed["sell_price"] == pytest.approx(1000.0)
    assert dumped["sell_price"] == pytest.approx(400.0)


# ── the internal-blueprint filter, after the GLOB rewrite ────────────────────
#
# `find_blueprint` used five `NOT GLOB` clauses to drop CCP's test/QA/tournament
# blueprints. GLOB is SQLite-only and `LIKE` cannot replace it: SQLite's LIKE
# ignores case and Postgres's does not, so one spelling means two things. The
# filter is a Python predicate now, which is identical on every backend and can
# be called directly — these are what say it still means what it meant.

def test_the_internal_blueprint_filter_catches_what_it_used_to():
    from app.bom.resolver import is_internal_blueprint_name as bad

    assert bad("Test Tungsten Carbide Blueprint")
    assert bad("Tournament Rifter Blueprint")
    assert bad("QA Damage Control Blueprint")
    assert bad("Something TEST Blueprint")
    assert bad("Foo TEST Bar Blueprint")


def test_the_filter_stays_case_sensitive():
    """The reason GLOB was there at all. A case-insensitive reading throws away
    real items, and 'Protest' is the example that made it GLOB in the first
    place — matching '%TEST%' loosely would delete it."""
    from app.bom.resolver import is_internal_blueprint_name as bad

    assert not bad("Protest Blueprint"), "a real item was filtered out"
    assert not bad("Contest Trophy Blueprint")
    assert not bad("test drive blueprint"), "lowercase is not CCP's convention"
    assert not bad("Latest Model Blueprint")


# ── The acquisition broker fee ───────────────────────────────────────────────
#
# This board is the surface most affected by it: `raw_input_basis` defaults to
# "buy", meaning the model assumes you place buy orders for moon materials —
# which costs a broker fee that `taxes.py` did not charge. Every reaction's
# input cost was understated, so every margin on the page was flattered.

def _tc_row(conn, path, **defaults):
    from app.web.app_defaults import get_defaults, save_defaults
    from app.web.reactions_helper import build_board
    if defaults:
        save_defaults(conn, defaults)
    get_defaults(conn)
    return next(r for r in build_board(conn, path)["rows"]
                if r["type_id"] == TUNGSTEN_CARBIDE)


def _price_tc_inputs(conn, path, unit=10.0):
    from app.bom.resolver import BOMResolver
    resolver = BOMResolver(connect_to_path(path))
    bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
    for mat in resolver.get_materials(bp["blueprint_type_id"], "reaction"):
        _price(conn, mat["material_type_id"], unit, buy=unit)
    _price(conn, TUNGSTEN_CARBIDE, 500.0, buy=500.0)


def test_reaction_inputs_carry_the_acquisition_fee(db):
    """Same quoted price on both sides, so the only thing that can move the
    input cost between bases is the broker fee."""
    path, conn = db
    _price_tc_inputs(conn, path)

    instant = _tc_row(conn, path, raw_input_basis="sell",
                      intermediate_input_basis="sell", broker_relations_skill=0)
    on_orders = _tc_row(conn, path, raw_input_basis="buy",
                        intermediate_input_basis="buy", broker_relations_skill=0)

    assert on_orders["material_cost"] > instant["material_cost"], (
        "reaction inputs on the buy basis were costed as free to acquire")


def test_the_output_is_not_charged_an_acquisition_fee(db):
    """The output is sold, not bought. Charging it the buy-side fee as well
    would double-count against `selling_cost`, which already prices that side."""
    path, conn = db
    _price_tc_inputs(conn, path)

    row = _tc_row(conn, path, raw_input_basis="buy",
                  intermediate_input_basis="buy", broker_relations_skill=0)

    assert row["sell_price"] == pytest.approx(500.0), (
        "the sale price was marked up by the acquisition fee")
