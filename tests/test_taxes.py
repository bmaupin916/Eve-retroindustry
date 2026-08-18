"""Market sales tax and broker's fee.

Rates verified 2026-08-18 against the Version 22.02 patch note (2025-03-12,
"Sales Tax has been increased from 4% to 7.5%") and the EVE University *Tax*
page. Note that EVE Uni's *Trading* page carries a stale worked total
("between 5.1% and 11%") that contradicts the formula printed beside it; the
formulas are authoritative, the prose totals are not.
"""
from __future__ import annotations

import pytest

from app.market.taxes import (
    SellingCosts, broker_fee_rate, order_broker_fee, sales_tax_rate,
    selling_costs, MIN_BROKER_FEE,
)


# ── sales tax ──────────────────────────────────────────────────────────────
def test_sales_tax_base_and_max_skill():
    assert sales_tax_rate(0) == pytest.approx(0.075)
    # 11% off the base per level -> 45% of 7.5% at level V. The wiki rounds
    # this to "3.37%".
    assert sales_tax_rate(5) == pytest.approx(0.03375)


def test_accounting_is_clamped_not_extrapolated():
    """A hand-edited config claiming Accounting 10 must not invent a negative
    tax and pay the trader for selling."""
    assert sales_tax_rate(99) == sales_tax_rate(5)
    assert sales_tax_rate(-3) == sales_tax_rate(0)
    assert sales_tax_rate(None) == sales_tax_rate(0)
    assert sales_tax_rate("nonsense") == sales_tax_rate(0)


# ── broker fee ─────────────────────────────────────────────────────────────
def test_npc_broker_fee_floor_confirms_the_coefficients():
    """EVE Uni states the NPC minimum is 1% at Broker Relations V with maximum
    faction and corp standings. Landing exactly on 1% confirms the per-level
    and per-standing terms independently of the base rate."""
    assert broker_fee_rate(broker_relations=5) == pytest.approx(0.015)
    assert broker_fee_rate(
        broker_relations=5, faction_standing=10, corp_standing=10
    ) == pytest.approx(0.01)


def test_negative_standings_cost_more():
    assert broker_fee_rate(faction_standing=-10) > broker_fee_rate()


def test_upwell_ignores_skills_and_standings():
    """Skills do not apply in player structures. Quietly applying Broker
    Relations here would understate the cost of trading in nullsec."""
    flat = broker_fee_rate(venue="upwell", broker_relations=5,
                           faction_standing=10, corp_standing=10)
    assert flat == pytest.approx(0.005)
    assert broker_fee_rate(venue="upwell", structure_fee_pct=1.0) == pytest.approx(0.015)


# ── the two selling methods ────────────────────────────────────────────────
def test_listing_an_order_pays_both_costs():
    c = selling_costs({"accounting_skill": 0, "broker_relations_skill": 0})
    assert c.pct == pytest.approx(10.5)


def test_selling_into_buy_orders_pays_no_broker_fee():
    """The distinction that makes the number honest: dumping to buy orders
    costs sales tax only."""
    c = selling_costs({"accounting_skill": 5, "sales_method": "immediate"})
    assert c.broker_fee == 0.0
    assert c.pct == pytest.approx(3.375)


def test_max_skills_and_standings_is_the_cheapest_case():
    c = selling_costs({"accounting_skill": 5, "broker_relations_skill": 5,
                       "faction_standing": 10, "corp_standing": 10})
    assert c.pct == pytest.approx(4.375)


def test_unknown_method_falls_back_to_the_expensive_one():
    """An unrecognised value must not silently zero the costs."""
    c = selling_costs({"sales_method": "wishful thinking"})
    assert c.method == "orders"
    assert c.broker_fee > 0


# ── applying it ────────────────────────────────────────────────────────────
def test_on_and_net_are_complements():
    c = SellingCosts(sales_tax=0.03375, broker_fee=0.015)
    assert c.on(1_000_000) == pytest.approx(48_750)
    assert c.net(1_000_000) == pytest.approx(951_250)
    assert c.net(None) is None


def test_order_fee_floor_applies_per_order_not_per_unit():
    """100 ISK minimum. It bites on a whole order, which is why per-unit margin
    maths deliberately does not apply it."""
    assert order_broker_fee(1_000, 0.03) == pytest.approx(MIN_BROKER_FEE)
    assert order_broker_fee(1_000_000, 0.03) == pytest.approx(30_000)
    assert order_broker_fee(0, 0.03) == 0.0


# ── round trip through the settings table ──────────────────────────────────
def test_settings_round_trip_reaches_the_calculation():
    """The keys the Settings page posts must survive storage and land in the
    rates. A typo in DEFAULTS would silently leave the pessimistic default in
    place, which looks like "the setting did nothing"."""
    import sqlite3
    from app.web.app_defaults import get_defaults, save_defaults

    conn = sqlite3.connect(":memory:")
    assert selling_costs(get_defaults(conn)).pct == pytest.approx(10.5)

    saved = save_defaults(conn, {
        "accounting_skill": "5",
        "broker_relations_skill": "5",
        "faction_standing": "10",
        "corp_standing": "10",
        "sell_venue": "npc",
        "sales_method": "orders",
    })
    assert selling_costs(saved).pct == pytest.approx(4.375)

    saved = save_defaults(conn, {"sales_method": "immediate"})
    assert selling_costs(saved).broker_fee == 0.0
    assert selling_costs(saved).pct == pytest.approx(3.375)


def test_upwell_settings_ignore_the_stored_skills():
    """Switching venue must drop the skill-based discount, not keep it."""
    import sqlite3
    from app.web.app_defaults import save_defaults

    conn = sqlite3.connect(":memory:")
    saved = save_defaults(conn, {
        "accounting_skill": "5", "broker_relations_skill": "5",
        "sell_venue": "upwell", "structure_broker_pct": "1.0",
    })
    c = selling_costs(saved)
    assert c.broker_fee == pytest.approx(0.015)     # 0.5% SCC + 1% owner, no skills
    assert c.sales_tax == pytest.approx(0.03375)    # Accounting still applies
