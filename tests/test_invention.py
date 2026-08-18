"""Invention cost model.

Runs against the committed ``sde_base.db``, because the point of most of these
is that the SDE answers questions a rule of thumb gets wrong.

Success chances are cross-checked against EVE University's published figures.
The best-known one is T2 modules at max skills with no decryptor: 49.6%.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

import pytest

from app.manufacturing import invention as inv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDE = os.path.join(REPO, "sde_base.db")

DAMAGE_CONTROL_II = 2048      # T2 module  — 10 runs per BPC
CRANE = 12729                 # T2 ship    — 1 run per BPC
ISHTAR = 12005                # T2 cruiser — Gallente, the name-matching trap
NERGAL = 52250                # Triglavian — the datacore with no dogma record
TRITANIUM = 34                # not invented at all


@pytest.fixture(scope="module")
def sde(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("inv") / "sde.db")
    shutil.copy2(SDE, path)
    conn = sqlite3.connect(path)
    yield conn
    conn.close()


# ── the formula ────────────────────────────────────────────────────────────
def test_max_skills_on_a_t2_module_is_the_published_496_percent():
    """0.34 x (1 + 10/30 + 5/40) = 0.4958. The figure every invention guide
    quotes, and the anchor for the whole model."""
    assert inv.success_chance(0.34, encryption_level=5,
                              science_levels=(5, 5)) == pytest.approx(0.49583, abs=1e-5)


def test_untrained_is_just_the_base_chance():
    assert inv.success_chance(0.34) == pytest.approx(0.34)


def test_each_skill_contributes_at_its_own_rate():
    """Science is 3.333% of base per level, encryption 2.5%."""
    base = inv.success_chance(0.30)
    one_science = inv.success_chance(0.30, science_levels=(1,))
    one_encryption = inv.success_chance(0.30, encryption_level=1)
    assert one_science - base == pytest.approx(0.30 / 30)
    assert one_encryption - base == pytest.approx(0.30 / 40)


def test_chance_cannot_exceed_certainty():
    """Sleeper Technical Schematics multiplies by 5, which would otherwise
    report a 170% chance."""
    wild = inv.Decryptor(name="Sleeper Technical Schematics", probability_mult=5.0)
    assert inv.success_chance(0.34, 5, (5, 5), decryptor=wild) == 1.0


def test_skill_levels_are_clamped():
    assert inv.success_chance(0.34, 99, (99, 99)) == inv.success_chance(0.34, 5, (5, 5))
    assert inv.success_chance(0.34, -1, (None, "x")) == pytest.approx(0.34)


def test_runs_per_bpc_never_drops_below_one():
    """Process Decryptor adds 0 runs; nothing may produce a 0-run BPC."""
    assert inv.runs_per_bpc(10) == 10
    assert inv.runs_per_bpc(1, inv.Decryptor(run_modifier=9.0)) == 10
    assert inv.runs_per_bpc(1, inv.Decryptor(run_modifier=-5.0)) == 1
    assert inv.runs_per_bpc(0) == 1


# ── reading the SDE ────────────────────────────────────────────────────────
def test_runs_per_bpc_comes_from_the_blueprint_not_a_rule_of_thumb(sde):
    """"10 runs, or 1 for ships and rigs" is close but not what the data says.
    The whole invention cost is divided by this number."""
    assert inv.find_recipe(sde, DAMAGE_CONTROL_II).base_runs == 10
    assert inv.find_recipe(sde, CRANE).base_runs == 1


def test_a_t1_item_has_no_invention_recipe(sde):
    """None means "not invented", which is how a caller tells that apart from
    an invention cost that happens to be zero."""
    assert inv.find_recipe(sde, TRITANIUM) is None


def test_gallente_science_skills_resolve_despite_the_name_mismatch(sde):
    """The datacore is "Gallentean Starship Engineering" and the skill is
    "Gallente Starship Engineering". Matching on names drops one of the two
    science skills for every Amarr and Gallente T2 ship, costing ~4 points of
    success chance and overstating cost accordingly."""
    recipe = inv.find_recipe(sde, ISHTAR)
    assert len(recipe.science_skill_ids) == 2
    assert recipe.encryption_skill_id is not None
    assert inv.success_chance(recipe.base_probability, 5, (5, 5)) == pytest.approx(0.3792, abs=1e-3)


def test_a_datacore_with_no_dogma_record_still_resolves(sde):
    """Datacore - Triglavian Quantum Engineering carries no dogma at all, so
    the importer falls back to its name, which matches its skill exactly."""
    recipe = inv.find_recipe(sde, NERGAL)
    assert len(recipe.science_skill_ids) == 2


def test_every_invented_product_resolves_two_science_skills(sde):
    """The property that caught two separate bugs. If this regresses, some
    class of item is being quietly underrated."""
    products = [r[0] for r in sde.execute(
        "SELECT DISTINCT p.product_type_id FROM sde_blueprint_products p "
        "WHERE p.activity='manufacturing' AND p.blueprint_type_id IN "
        "(SELECT product_type_id FROM sde_blueprint_products WHERE activity='invention')")]
    assert len(products) > 1000
    short = [p for p in products
             if len((inv.find_recipe(sde, p) or inv.InventionRecipe(0, 0, 0, 0, 0)).science_skill_ids) != 2]
    assert short == []


def test_decryptors_are_loaded_from_the_sde(sde):
    """64 of them, not the 8 every guide lists."""
    all_of_them = inv.list_decryptors(sde)
    assert len(all_of_them) > 50
    by_name = {d.name: d for d in all_of_them}
    accelerant = by_name["Accelerant Decryptor"]
    assert accelerant.probability_mult == pytest.approx(1.2)
    assert accelerant.run_modifier == pytest.approx(1.0)
    assert accelerant.me_modifier == pytest.approx(2.0)
    assert accelerant.te_modifier == pytest.approx(10.0)


# ── the cost ───────────────────────────────────────────────────────────────
def _levels(recipe, science=5, encryption=5):
    levels = {s: science for s in recipe.science_skill_ids}
    if recipe.encryption_skill_id:
        levels[recipe.encryption_skill_id] = encryption
    return levels


def test_cost_carries_the_failed_attempts(sde):
    """Datacores are consumed whether the job succeeds or fails, so one
    success costs every attempt before it."""
    recipe = inv.find_recipe(sde, DAMAGE_CONTROL_II)
    prices = {tid: 100_000.0 for tid, _n, _q in recipe.datacores}
    cost = inv.compute_cost(recipe, prices, _levels(recipe))
    assert cost.expected_attempts == pytest.approx(1 / cost.success_chance)
    assert cost.per_bpc == pytest.approx(cost.per_attempt * cost.expected_attempts)
    assert cost.per_bpc > cost.per_attempt          # failures are not free


def test_a_t2_ship_carries_its_whole_invention_cost_on_one_hull(sde):
    """The misranking this model exists to prevent: the same datacore spend is
    spread over 10 modules but lands entirely on 1 ship."""
    ship = inv.find_recipe(sde, CRANE)
    module = inv.find_recipe(sde, DAMAGE_CONTROL_II)
    price = 100_000.0
    ship_cost = inv.compute_cost(
        ship, {t: price for t, _n, _q in ship.datacores}, _levels(ship))
    module_cost = inv.compute_cost(
        module, {t: price for t, _n, _q in module.datacores}, _levels(module))
    assert ship_cost.runs_per_bpc == 1
    assert module_cost.runs_per_bpc == 10
    assert module_cost.per_unit == pytest.approx(module_cost.per_bpc / 10)
    assert ship_cost.per_unit == pytest.approx(ship_cost.per_bpc)


def test_an_unpriced_datacore_is_reported_not_treated_as_free(sde):
    """Same rule as the materials: a silently-free input understates cost and
    overstates profit."""
    recipe = inv.find_recipe(sde, DAMAGE_CONTROL_II)
    cost = inv.compute_cost(recipe, {}, _levels(recipe))
    assert cost.unpriced
    assert cost.datacore_cost == 0.0


def test_better_skills_cost_less(sde):
    recipe = inv.find_recipe(sde, CRANE)
    prices = {tid: 100_000.0 for tid, _n, _q in recipe.datacores}
    trained = inv.compute_cost(recipe, prices, _levels(recipe, 5, 5))
    untrained = inv.compute_cost(recipe, prices, _levels(recipe, 0, 0))
    assert untrained.per_unit > trained.per_unit


def test_a_decryptor_trades_isk_and_odds_for_runs(sde):
    """Augmentation halves the chance and costs ISK, but adds 9 runs to the
    BPC — which is why it can still come out cheaper per unit."""
    recipe = inv.find_recipe(sde, CRANE)
    prices = {tid: 100_000.0 for tid, _n, _q in recipe.datacores}
    plain = inv.compute_cost(recipe, prices, _levels(recipe))
    aug = next(d for d in inv.list_decryptors(sde) if d.name == "Augmentation Decryptor")
    with_dec = inv.compute_cost(recipe, prices, _levels(recipe),
                                decryptor=aug, decryptor_price=1_000_000.0)
    assert with_dec.success_chance < plain.success_chance
    assert with_dec.runs_per_bpc == plain.runs_per_bpc + 9
    assert with_dec.per_unit < plain.per_unit
