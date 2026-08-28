"""Freight on `/plan` — the flat fee, charged once at each boundary.

The rule, from the person who runs the operation: *you either calculate the
import in or the export out, you don't calculate between runs.* A planning run
that needs 10,000 m³ brought in at 300 ISK/m³ costs 3,000,000 ISK, and that
lands in the build cost beside the job installation fee.

Two figures for import, not one, mirroring the two profit lines the page has
always shown: the market table buys every material, the stock table buys only
what you are short of, and **you only haul what you buy**.

The first assertion here is the one that matters for anyone who has not
configured a rate: with both rates at their default of zero, nothing about this
page changes — no row, no cost, no arithmetic. An always-zero row is the mistake
the reactions board's Sell Advantage column already made.
"""
from __future__ import annotations

import pytest

from tests.test_plan_profit_table import _plan, _plan_view  # noqa: F401

# Something with a real bill of materials, so there is volume to haul.
PRODUCT = "Rifter"


@pytest.fixture
def rate(client, app_module):
    """Set the freight rates for one test, and put them back afterwards."""
    from app.db.conn import connect
    from app.web import app_defaults

    def _set(import_isk: float, export_isk: float):
        with connect() as conn:
            app_defaults.save_defaults(conn, {
                "freight_import_isk_m3": import_isk,
                "freight_export_isk_m3": export_isk,
            })

    _set(0.0, 0.0)
    yield _set
    _set(0.0, 0.0)


def _fees(client, product=PRODUCT, qty="1") -> dict:
    _r, result = _plan_view(client, product, qty)
    return (result or {}).get("fees") or {}


def test_no_rate_configured_changes_nothing(client, rate):
    """The default. Building and selling in one station pays no freight."""
    rate(0.0, 0.0)
    fees = _fees(client)
    assert fees.get("import_cost") == 0.0
    assert fees.get("export_cost") == 0.0


def test_no_rate_configured_renders_no_freight_row(client, rate):
    """A row that is always zero is worse than no row."""
    rate(0.0, 0.0)
    r = _plan(client, PRODUCT)
    assert "Import freight" not in r.text
    assert "Export freight" not in r.text


def test_import_is_volume_times_rate(client, rate):
    """The flat fee, and nothing cleverer: m³ in × ISK per m³."""
    rate(300.0, 0.0)
    fees = _fees(client)
    assert fees["import_m3"] > 0, "the plan hauls nothing, so there is no test here"
    assert fees["import_cost"] == pytest.approx(fees["import_m3"] * 300.0)


def test_export_is_volume_times_rate(client, rate):
    rate(0.0, 300.0)
    fees = _fees(client)
    assert fees["export_m3"] > 0
    assert fees["export_cost"] == pytest.approx(fees["export_m3"] * 300.0)


def test_the_rates_are_independent(client, rate):
    """Setting one leg must not charge the other."""
    rate(300.0, 0.0)
    fees = _fees(client)
    assert fees["import_cost"] > 0 and fees["export_cost"] == 0.0


def test_you_only_haul_what_you_buy(client, rate):
    """The stock table charges only the missing materials.

    Same reason its material cost differs from the market table's: what you
    already hold does not need bringing in.
    """
    rate(300.0, 0.0)
    fees = _fees(client)
    # Strictly less, not "at most". The fixture character holds 10,000 of
    # every mineral, so a single Rifter is short of nothing — and `<=` is
    # satisfied by a version that charges the full haul on both tables,
    # which is exactly the mutation that used to survive this test.
    assert fees["import_m3"] > 0
    assert fees["import_m3_stock"] < fees["import_m3"]
    assert fees["import_cost_stock"] == pytest.approx(
        fees["import_m3_stock"] * 300.0)


def test_freight_appears_as_its_own_cost_line(client, rate):
    """Not folded into materials: it is the one figure here that comes from a
    setting rather than the market, and a cost you cannot see is one you
    cannot check."""
    rate(300.0, 300.0)
    r = _plan(client, PRODUCT)
    assert "Import freight" in r.text
    assert "Export freight" in r.text


def test_the_quantity_scales_the_haul(client, rate):
    """Ten of something needs ten times the materials brought in."""
    rate(300.0, 300.0)
    one = _fees(client, qty="1")
    ten = _fees(client, qty="10")
    assert ten["import_m3"] == pytest.approx(one["import_m3"] * 10, rel=0.2), (
        f"{one['import_m3']:,.1f} m³ in for one and {ten['import_m3']:,.1f} "
        "for ten do not scale")
    # The export leg too: ten hulls ship ten hulls.
    assert ten["export_m3"] == pytest.approx(one["export_m3"] * 10), (
        f"{one['export_m3']:,.1f} m³ out for one and {ten['export_m3']:,.1f} "
        "for ten do not scale")
