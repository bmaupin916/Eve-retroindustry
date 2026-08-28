"""Import freight — the flat fee on what you actually haul in.

The design doc listed this as unresolvable: *"which inputs get hauled differs
between the two costing models — the whole moon-goo chain under raw, only the
intermediates under buy — and guessing would quietly bias every margin."*

The rule that resolves it, stated by the person who runs the operation: **you
charge the import in or the export out, never between runs.** So the difference
between the two models is not ambiguity, it is the answer — each model charges
freight on its own purchased set, and charging one but not the other is what
would bias Build Advantage.

Which makes the recursion rule exact, and it is what
`test_a_built_intermediate_is_not_hauled` pins:

* a material you **buy** contributes its own packaged m³
* a material you **build** contributes its inputs' m³ instead

Both rates default to 0, so none of this moves a number until somebody sets one.
"""
from __future__ import annotations

import pytest

from tests.test_reactions import (NITROGEN_FUEL_BLOCK, TUNGSTEN_CARBIDE,  # noqa: F401
                                 _price, db, connect_to_path)


def _resolver_ctx(path, conn):
    from app.bom.resolver import BOMResolver
    from app.manufacturing.margins import _station_context
    from app.web.app_defaults import get_defaults

    resolver = BOMResolver(connect_to_path(path))
    ctx = _station_context(conn, get_defaults(conn))
    return resolver, ctx


def test_no_rate_configured_charges_nothing(db):
    """The default. Building and selling in one station pays no freight."""
    path, conn = db
    from app.web.reactions_helper import raw_unit_cost_and_volume, _prices

    resolver, ctx = _resolver_ctx(path, conn)
    try:
        bp = resolver.find_blueprint(NITROGEN_FUEL_BLOCK)
        mats = resolver.get_materials(bp["blueprint_type_id"], "manufacturing")
        for mat in mats:
            _price(conn, mat["material_type_id"], 100.0)
        prices = _prices(conn, {m["material_type_id"] for m in mats}, "buy")

        # No `volumes` map at all is the same statement as a zero rate.
        pair = raw_unit_cost_and_volume(
            resolver, NITROGEN_FUEL_BLOCK, prices, ctx, {}, set())
    finally:
        resolver.close()

    assert pair is not None
    assert pair[1] == 0.0


def test_a_bought_material_is_hauled_at_its_own_volume(db):
    """A leaf you purchase: quantity times packaged m³, nothing cleverer."""
    path, conn = db
    from app.web.reactions_helper import raw_unit_cost_and_volume, _prices

    resolver, ctx = _resolver_ctx(path, conn)
    try:
        bp = resolver.find_blueprint(NITROGEN_FUEL_BLOCK)
        mats = resolver.get_materials(bp["blueprint_type_id"], "manufacturing")
        for mat in mats:
            _price(conn, mat["material_type_id"], 100.0)
        prices = _prices(conn, {m["material_type_id"] for m in mats}, "buy")

        # One m³ per unit of every input makes the expected volume equal to the
        # expected quantity, so the assertion is about the walk, not arithmetic.
        volumes = {m["material_type_id"]: 1.0 for m in mats}
        pair = raw_unit_cost_and_volume(
            resolver, NITROGEN_FUEL_BLOCK, prices, ctx, {}, set(), volumes)

        expected_qty = sum(
            resolver._apply_me(m["quantity"], 1, 0.0, 1.0, runs_per_job=None)
            for m in mats)
        per_run = bp["product_qty"]
    finally:
        resolver.close()

    assert pair is not None
    assert pair[1] == pytest.approx(expected_qty / per_run, rel=0.5)


def test_a_built_intermediate_is_not_hauled(db):
    """The rule: you charge the boundary, never between your own runs.

    Tungsten Carbide is reacted from Nitrogen Fuel Blocks, which are
    themselves manufactured — so the block is an intermediate you *make*,
    and nothing hauls it.

    Asserted by changing only the block's own volume and requiring the
    answer not to move. Two earlier versions of this test were weaker and
    both let a mutation reversing the rule pass: a one-level tree, where
    "its own volume" and "its inputs' volume" are the same number; and then
    a threshold, which the per-unit division by a reaction's run yield
    brought 5,000,000 m3 comfortably under.
    """
    path, conn = db
    from app.bom.resolver import BOMResolver
    from app.web.reactions_helper import raw_unit_cost_and_volume, _prices

    seed = BOMResolver(connect_to_path(path))
    try:
        leaves = list(seed.resolve(TUNGSTEN_CARBIDE, 1).aggregate_leaves())
    finally:
        seed.close()
    for leaf_id in leaves:
        _price(conn, leaf_id, 100.0)
    _price(conn, NITROGEN_FUEL_BLOCK, 100.0)

    ids = {r[0] for r in conn.exec_driver_sql(
        "SELECT type_id FROM market_price_cache")}

    resolver, ctx = _resolver_ctx(path, conn)
    try:
        prices = _prices(conn, ids, "buy")

        def hauled(block_volume: float) -> float:
            volumes = {tid: 1.0 for tid in ids}
            volumes[NITROGEN_FUEL_BLOCK] = block_volume
            pair = raw_unit_cost_and_volume(
                resolver, TUNGSTEN_CARBIDE, prices, ctx, {}, set(), volumes)
            assert pair is not None
            return pair[1]

        ordinary = hauled(1.0)
        absurd = hauled(1_000_000.0)

        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
        mats = resolver.get_materials(bp["blueprint_type_id"], "reaction")
        block_qty = next(
            (m["quantity"] for m in mats
             if m["material_type_id"] == NITROGEN_FUEL_BLOCK), 0)
    finally:
        resolver.close()

    assert block_qty, "fixture assumes Tungsten Carbide consumes fuel blocks"
    assert ordinary > 0.0, "nothing was hauled at all — the walk found no leaves"
    assert absurd == pytest.approx(ordinary), (
        "the intermediate's own volume changed the answer, so it is being "
        f"hauled: {ordinary:,.2f} -> {absurd:,.2f} m3 per unit")

def test_the_cost_wrapper_still_answers_only_the_cost(db):
    """`raw_unit_cost` is a wrapper now; four existing call sites rely on it."""
    path, conn = db
    from app.web.reactions_helper import (raw_unit_cost,
                                          raw_unit_cost_and_volume, _prices)

    resolver, ctx = _resolver_ctx(path, conn)
    try:
        bp = resolver.find_blueprint(NITROGEN_FUEL_BLOCK)
        mats = resolver.get_materials(bp["blueprint_type_id"], "manufacturing")
        for mat in mats:
            _price(conn, mat["material_type_id"], 100.0)
        prices = _prices(conn, {m["material_type_id"] for m in mats}, "buy")

        cost = raw_unit_cost(resolver, NITROGEN_FUEL_BLOCK, prices, ctx, {}, set())
        pair = raw_unit_cost_and_volume(
            resolver, NITROGEN_FUEL_BLOCK, prices, ctx, {}, set())
    finally:
        resolver.close()

    assert cost is not None and pair is not None
    assert cost == pytest.approx(pair[0])


def test_an_unpriced_input_still_has_no_cost_and_no_volume(db):
    """Missing a price must not become a free haul of zero cubic metres."""
    path, conn = db
    from app.web.reactions_helper import raw_unit_cost_and_volume

    resolver, ctx = _resolver_ctx(path, conn)
    try:
        unpriced: set[str] = set()
        pair = raw_unit_cost_and_volume(
            resolver, NITROGEN_FUEL_BLOCK, {}, ctx, {}, unpriced,
            {NITROGEN_FUEL_BLOCK: 5.0})
    finally:
        resolver.close()

    assert pair is None
    assert unpriced, "nothing recorded as unpriced"


def test_the_direct_input_model_hauls_everything_it_buys(db):
    """Under buy-the-intermediates every direct input is purchased, so all of
    it is hauled — which is exactly why the two models differ.

    A reaction product, because `_input_cost` asks the resolver for
    "reaction" materials and a manufacturing blueprint returns none.
    """
    path, conn = db
    from app.web.reactions_helper import _input_cost, _prices

    resolver, ctx = _resolver_ctx(path, conn)
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
        mats = resolver.get_materials(bp["blueprint_type_id"], "reaction")
        assert mats, "fixture product has no reaction inputs"
        for mat in mats:
            _price(conn, mat["material_type_id"], 100.0)
        prices = _prices(conn, {m["material_type_id"] for m in mats}, "buy")
        volumes = {m["material_type_id"]: 1.0 for m in mats}

        _cost, _fee, _unpriced, hauled = _input_cost(
            resolver, conn, TUNGSTEN_CARBIDE, bp, prices, ctx, volumes)

        expected = sum(
            resolver._apply_me(m["quantity"], 1, 0.0, 1.0, runs_per_job=None)
            for m in mats)
    finally:
        resolver.close()

    assert hauled == pytest.approx(expected, rel=0.5)


def test_input_cost_without_volumes_reports_no_haul(db):
    """The parameter is optional; omitting it must not invent a volume."""
    path, conn = db
    from app.web.reactions_helper import _input_cost, _prices

    resolver, ctx = _resolver_ctx(path, conn)
    try:
        bp = resolver.find_blueprint(TUNGSTEN_CARBIDE)
        mats = resolver.get_materials(bp["blueprint_type_id"], "reaction")
        for mat in mats:
            _price(conn, mat["material_type_id"], 100.0)
        prices = _prices(conn, {m["material_type_id"] for m in mats}, "buy")

        _cost, _fee, _unpriced, hauled = _input_cost(
            resolver, conn, TUNGSTEN_CARBIDE, bp, prices, ctx)
    finally:
        resolver.close()

    assert hauled == 0.0
