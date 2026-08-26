"""`app/web/routers/plan.py`, before it moves onto the portable query layer.

Eighteen raw statements, and mutating them against the existing tests caught
**2 of 19**. That is not a criticism of those tests — `test_orders_cache.py`
pins which fetchers `/plan` may call, `test_plan_connection_lifetime.py` pins
how long it holds a connection, `test_job_splitting.py` pins the scheduler.
None of them is about what the SQL returns, and the SQL here decides what the
page says a build costs.

**Two of these statements cannot run on Postgres at all**, which is the reason
this file exists rather than a nice-to-have:

* `COLLATE NOCASE` appears three times in `_resolve_product_local` — the
  function that turns what you type into a type id. Postgres has no such
  collation. `app/bom/resolver.py` already carries a note about `GLOB` being
  SQLite-only; this is the same class, found later.
* Every `IN ({ph})` needs an expanding bindparam rather than a placeholder
  swap, and there are eight of them.

The conversion changes case-folding in one respect worth stating plainly:
SQLite's `NOCASE` folds **ASCII only**, while Postgres' `LOWER()` is
locale-aware and folds Unicode. EVE type names are effectively ASCII, so
nothing here changes — but it is a behaviour difference, not a translation, and
pretending otherwise is how the next surprise gets built.

A hazard specific to this module: `_science_skill_mult` memoises its two
lookups on the **function object**, so they run once per process. Any test
about them has to reset those caches or it is asserting on whatever an earlier
test warmed — the same shape as the prepared-statement cache that hid the
route-jump chunking bug in v0.9.68.
"""
from __future__ import annotations

import pytest

from app.web.routers import plan as plan_router

# ── real SDE ids ─────────────────────────────────────────────────────────────
BANTAM = 582                 # a T1 frigate: short BOM, real blueprint
BANTAM_BLUEPRINT = 683
TRITANIUM = 34
JITA = 60003760
JITA_SYSTEM = 30000142

INDUSTRY = 3380              # excluded from the per-blueprint skill list
ADVANCED_INDUSTRY = 3388


def _post(client, **overrides) -> dict:
    """Submit the plan form and return the template context.

    Goes through the real route — the SQL under test only runs on that path —
    and captures the context the way the PI tests do, because the numbers this
    file is about never appear in the HTML in a form worth asserting on.
    """
    data = {"product": "Bantam", "qty": "1", "station": str(JITA),
            "mode": "full", "runs_per_job": "0", "form_me": "0"}
    data.update({k: str(v) for k, v in overrides.items()})

    captured = {}
    original = plan_router._tr

    def spy(name, request, context):
        captured["view"] = context
        return original(name, request, context)

    plan_router._tr = spy
    try:
        response = client.post("/plan", data=data)
        assert response.status_code == 200, response.text[:400]
    finally:
        plan_router._tr = original
    return captured["view"]


def _result(client, **overrides) -> dict:
    view = _post(client, **overrides)
    assert view["error"] is None, f"the plan failed: {view['error']}"
    assert view["result"] is not None, "the plan produced no result at all"
    return view["result"]


@pytest.fixture(autouse=True)
def _cold_skill_caches():
    """Drop `_science_skill_mult`'s process-level memos before each test.

    Without this, whichever test ran first decides what every later one sees:
    the bonus table and the skill names are cached on the function object for
    the life of the interpreter. A test that asserts on them while the cache is
    warm is asserting on history.
    """
    for attr in ("_bonus_cache", "_name_cache"):
        if hasattr(plan_router._science_skill_mult, attr):
            delattr(plan_router._science_skill_mult, attr)
    yield


# ── _resolve_product_local: what you type becomes a type id ──────────────────

def test_a_typed_product_name_resolves_to_the_product(client):
    """The happy path, and the one every other assertion here depends on."""
    result = _result(client)

    assert result["product_type_id"] == BANTAM, (
        f"expected the Bantam, got {result['product_name']!r} "
        f"({result['product_type_id']})")


@pytest.mark.parametrize("typed", ["bantam", "BANTAM", "BaNtAm"])
def test_the_product_name_is_matched_case_insensitively(client, typed):
    """`COLLATE NOCASE` today, `LOWER(name) = :name` after the conversion.

    All three spellings must land on the same type. This is the assertion the
    conversion is judged by: drop the case-folding and an exact match on
    "bantam" finds nothing, the prefix and substring passes find nothing
    either, and the page answers "unknown product" for a ship that exists.

    Parametrised over three spellings rather than one because a single
    lowercase probe cannot tell case-folding from a lucky exact match.
    """
    result = _result(client, product=typed)

    assert result["product_type_id"] == BANTAM


def test_a_product_is_preferred_over_its_blueprint(client):
    """`_pick` prefers *producible* candidates.

    "Bantam" also prefix-matches "Bantam Blueprint", which is a thing you
    invent rather than a thing you build. Planning a build of the blueprint is
    a plan for the wrong item, and it is the exact confusion the producible
    filter exists to prevent.
    """
    result = _result(client, product="Bantam")

    assert result["product_type_id"] == BANTAM
    assert "Blueprint" not in result["product_name"], (
        f"planning resolved to {result['product_name']!r}")


def test_an_unknown_product_is_an_error_not_an_empty_plan(client):
    """The failure the resolver owes the user. An empty plan for a typo reads
    as "this is free to build"."""
    view = _post(client, product="Zzzz Not A Real Item Zzzz")

    assert view["result"] is None
    assert view["error"], "an unresolvable product produced neither result nor error"


# ── the bulk SDE prefetch that feeds the bill of materials ───────────────────

def test_the_plan_lists_the_materials_the_blueprint_consumes(client):
    """`sde_blueprint_materials`, prefetched for every blueprint in the tree.

    A Bantam is built from minerals. An empty materials list is not an error —
    it renders as a build that needs nothing, with a cost of zero and an
    infinite margin.
    """
    result = _result(client)

    assert result["materials"], "the plan lists no materials at all"
    names = {m["name"] for m in result["materials"]}
    assert "Tritanium" in names, f"a Bantam is built from minerals: {sorted(names)}"


def test_each_material_carries_its_group_name(client):
    """The `LEFT JOIN sde_groups` in `_plan_to_dict`.

    `group_name` is what the materials table groups rows by; the fallback is
    an em dash, so a broken join degrades into a flat, unsorted shopping list
    rather than an error.
    """
    result = _result(client)

    groups = {m["group_name"] for m in result["materials"]}
    assert groups != {"—"}, "every material lost its group name"
    tritanium = next(m for m in result["materials"] if m["type_id"] == TRITANIUM)
    assert tritanium["group_name"] == "Mineral", (
        f"Tritanium is a Mineral, not {tritanium['group_name']!r}")


def test_the_blueprint_carries_the_me_the_form_asked_for(client):
    """`sde_blueprints` supplies the times; the form supplies ME. Pinned
    together because the plan is wrong in different ways if either is lost."""
    result = _result(client, form_me=10)

    assert result["blueprint"] is not None, "no blueprint was found for the Bantam"
    assert result["blueprint"]["me"] == 10


def test_material_quantities_fall_when_me_rises(client):
    """Material efficiency is the whole point of the ME field, and it is
    applied to quantities that came out of `sde_blueprint_materials`. If that
    prefetch returned nothing both numbers would be zero and equal — so this
    also fails when the materials query breaks, in a way the presence check
    above cannot."""
    at_zero = _result(client, form_me=0)
    at_ten = _result(client, form_me=10)

    def _trit(result):
        return next(m for m in result["materials"] if m["type_id"] == TRITANIUM)["required"]

    assert _trit(at_zero) > 0
    assert _trit(at_ten) < _trit(at_zero), (
        f"ME 10 required {_trit(at_ten)} Tritanium against {_trit(at_zero)} at ME 0")


# ── the industry skills, which are looked up apart from the rest ─────────────

def test_the_required_industry_level_is_reported(client):
    """`skill_type_id IN (3380, 3388)` with `MAX(required_level)`.

    **The first version of this test asserted on `form_industry`, which this
    query does not feed.** That field carries the *character's* Industry level,
    read from the skill cache; the query fills `industry_required` — what the
    plan *needs*. Both are about Industry, both sit in the same response, and
    only one of them breaks when the query does. The mutation battery is how
    that came to light: 12 new tests bought exactly one extra catch, and this
    was one of the misses.
    """
    result = _result(client)

    assert "industry_required" in result, "the requirement was never computed"
    assert result["industry_required"] >= 1, (
        f"every manufacturing job needs Industry I, got "
        f"{result['industry_required']}")


def test_the_science_skill_list_excludes_industry(client):
    """The matching `NOT IN (3380, 3388)` on the per-blueprint skill prefetch.

    Industry and Advanced Industry are handled separately — they scale job time
    for the whole plan rather than gating one blueprint. Left in the
    per-blueprint list they are applied a *second* time by the science-skill
    multiplier, and the plan quietly reports a build faster than the game
    allows.

    Asserted on `all_science_skills`, which is the list that multiplier is
    derived from, rather than on the plan being non-empty — the previous
    version of this test asserted `result is not None`, which would have passed
    with the whole module deleted.
    """
    result = _result(client)

    listed = {s[0] if isinstance(s, (list, tuple)) else s.get("name")
              for s in result.get("all_science_skills", [])}

    assert "Industry" not in listed, (
        f"Industry leaked into the per-blueprint skill list: {sorted(listed)}")
    assert "Advanced Industry" not in listed


def test_the_job_fee_is_priced_from_the_prefetched_base_materials(client):
    """`sde_blueprint_materials` — and it does **not** feed the bill of
    materials, which is what the first version of this test assumed.

    It feeds EIV (Estimated Item Value): the sum of *base*, pre-ME material
    quantities at adjusted prices, which is what CCP's job-cost formula is
    charged on. A prefetch that returns nothing gives every job an EIV of zero,
    so `total_job_fee` becomes zero and the plan reports a build with no
    industry cost at all — cheaper than reality, with no error.

    Asserting Tritanium was in `materials` could never have caught that: the
    BOM comes from the resolver, not from here.
    """
    result = _result(client)

    fees = result.get("fees") or {}
    assert "total_job_fee" in fees, f"no job-fee breakdown in the result: {list(fees)}"
    assert fees["total_job_fee"] > 0, (
        "the plan charges nothing to run its jobs — EIV came back zero, which "
        "is what an empty base-material prefetch looks like")
