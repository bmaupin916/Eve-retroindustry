"""Job splitting — per-product run limits, and the scheduling they feed.

Splitting a job changes material totals, because EVE rounds ME once per job:
600 runs as one job is cheaper than ten jobs of 60. The first half of this file
pins `BOMResolver`'s existing splitting behaviour (it is shared with the margin
tracker, so a regression there is not local), the second covers the per-product
splits and the slot-constrained schedule built on top.
"""
from __future__ import annotations

import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDE = os.path.join(REPO, "sde_base.db")

WASP_II = 2436
MORPHITE = 11399


@pytest.fixture
def resolver():
    from app.bom.resolver import BOMResolver
    r = BOMResolver(SDE)
    yield r
    r.close()


# ── pinned behaviour (pre-existing, must not move) ───────────────────────────
@pytest.mark.parametrize("runs_per_job,expected", [
    (None, 56),   # one batched job: max(10, ceil(6 × 10 × 0.93)) = 56
    (1,    60),   # ten single-run jobs: each floors at 6 → 60
    (3,    57),   # three jobs of 3 + one of 1
    (5,    56),   # two jobs of 5
    (10,   56),   # exactly one job's worth
    (20,   56),   # J larger than the run count collapses to one job
])
def test_apply_me_splitting_is_unchanged(resolver, runs_per_job, expected):
    """The ME-per-job formula, pinned before per-product splits were added.

    Smaller jobs cost more materials — that is the whole reason a day-based
    split has to feed back into the materials list.
    """
    resolver.runs_per_job = runs_per_job
    assert resolver._apply_me(6, 10, 7) == expected


def test_smaller_jobs_never_cost_less(resolver):
    """Monotonic: splitting more finely can only raise material cost."""
    costs = []
    for runs_per_job in (None, 20, 10, 5, 3, 2, 1):
        resolver.runs_per_job = runs_per_job
        costs.append(resolver._apply_me(6, 10, 7))
    assert costs == sorted(costs)


# ── per-product splits ───────────────────────────────────────────────────────
def test_per_product_split_overrides_the_global_one():
    """A day-based split yields a different run count per product, so the
    resolver has to accept a map rather than one number for the whole tree."""
    from app.bom.resolver import BOMResolver

    r = BOMResolver(SDE, runs_per_job=None, runs_per_job_by_product={WASP_II: 1})
    try:
        assert r._runs_per_job_for(WASP_II) == 1        # the override
        assert r._runs_per_job_for(MORPHITE) is None    # falls back to global
    finally:
        r.close()


def test_per_product_split_changes_material_totals():
    """The split must actually reach the materials, not just the job list."""
    from app.bom.resolver import BOMResolver

    def morphite(by_product):
        r = BOMResolver(SDE, runs_per_job=None, runs_per_job_by_product=by_product)
        try:
            node = r.resolve(WASP_II, 10 * 5000, me=7)
            return next((q for t, (_n, q) in node.aggregate_leaves().items()
                         if t == MORPHITE), None)
        finally:
            r.close()

    batched = morphite(None)
    split = morphite({WASP_II: 1})
    assert batched is not None and split is not None
    assert split > batched, "splitting into single-run jobs must cost more Morphite"


def test_absent_map_leaves_behaviour_identical():
    """The margin tracker constructs BOMResolver without the new argument."""
    from app.bom.resolver import BOMResolver

    plain = BOMResolver(SDE, runs_per_job=None)
    mapped = BOMResolver(SDE, runs_per_job=None, runs_per_job_by_product={})
    try:
        a = plain.resolve(WASP_II, 5000, me=7).aggregate_leaves()
        b = mapped.resolve(WASP_II, 5000, me=7).aggregate_leaves()
        assert a == b
    finally:
        plain.close()
        mapped.close()


# ── splitting by day limit ───────────────────────────────────────────────────
def test_max_runs_per_job_uses_per_run_time_only():
    """The split size depends on per-run time, never on the total.

    That is what lets the caller pick split sizes before final run counts are
    known, and re-resolve the bill of materials against them in one extra pass.
    """
    from app.manufacturing.schedule import SECONDS_PER_DAY, max_runs_per_job

    hour = 3600
    assert max_runs_per_job(hour, 1.0) == 24          # 24 one-hour runs in a day
    assert max_runs_per_job(hour, 3.0) == 72
    assert max_runs_per_job(SECONDS_PER_DAY * 2, 1.0) == 1   # one run already overruns
    assert max_runs_per_job(hour, 0) is None          # 0 days = no splitting
    assert max_runs_per_job(0, 3.0) is None           # unknown time = no splitting


def test_split_runs_leaves_a_smaller_remainder_job():
    from app.manufacturing.schedule import split_runs

    assert split_runs(600, 60) == [60] * 10
    assert split_runs(125, 60) == [60, 60, 5]
    assert split_runs(30, 60) == [30]        # fits in one
    assert split_runs(30, None) == [30]      # unlimited
    assert split_runs(0, 60) == []


def test_the_worked_example_from_the_request():
    """600 runs at 10 days becomes ten 1-day jobs of 60 runs."""
    from app.manufacturing.schedule import max_runs_per_job, split_runs

    seconds_per_run = (10 * 86400) / 600            # 600 runs = 10 days
    per_job = max_runs_per_job(seconds_per_run, 1.0)
    assert per_job == 60
    assert split_runs(600, per_job) == [60] * 10


# ── slot-constrained scheduling ──────────────────────────────────────────────
def _job(seconds, activity="manufacturing", capital=False, runs=1):
    from app.manufacturing.schedule import Job
    return Job(type_id=1, name="x", activity=activity, runs=runs,
               seconds=seconds, is_capital=capital)


def test_unlimited_slots_reproduces_the_old_estimate():
    """0 slots must behave exactly as before — longest job wins — so an
    unconfigured install sees no change."""
    from app.manufacturing.schedule import SlotLimits, schedule_level

    jobs = [_job(100), _job(300), _job(200)]
    assert schedule_level(jobs, SlotLimits()) == 300


def test_limited_slots_serialise_the_overflow():
    from app.manufacturing.schedule import SlotLimits, schedule_level

    jobs = [_job(100) for _ in range(10)]
    assert schedule_level(jobs, SlotLimits(manufacturing=10)) == 100   # all at once
    assert schedule_level(jobs, SlotLimits(manufacturing=5)) == 200    # two waves
    assert schedule_level(jobs, SlotLimits(manufacturing=1)) == 1000   # one at a time


def test_reactions_and_manufacturing_run_concurrently():
    """Separate pools, so a level takes the longer of the two, not the sum."""
    from app.manufacturing.schedule import SlotLimits, schedule_level

    jobs = [_job(500, "reaction"), _job(300, "manufacturing")]
    limits = SlotLimits(manufacturing=1, reaction=1)
    assert schedule_level(jobs, limits) == 500


def test_capital_slots_are_a_subset_of_manufacturing_not_an_addition():
    """20 manufacturing slots with 10 capital-capable means at most 10
    concurrent capital jobs *out of* those 20 — not 30 slots.

    Ten capital jobs and twenty ordinary jobs, all 100s: the capital work fills
    the 10 capable slots in one wave, the 20 ordinary jobs have only the 10
    remaining slots free in that wave, so they take two. Total 200 — not the
    100 you'd get if capital slots were extra capacity.
    """
    from app.manufacturing.schedule import SlotLimits, schedule_level

    jobs = [_job(100, capital=True) for _ in range(10)]
    jobs += [_job(100) for _ in range(20)]
    limits = SlotLimits(manufacturing=20, capital=10)
    assert schedule_level(jobs, limits) == 200

    # Were they a separate pool, all 30 would fit at once and finish in 100.
    assert schedule_level(jobs, SlotLimits(manufacturing=30, capital=10)) == 100


def test_capital_capable_cannot_exceed_the_pool():
    """Configuring more capital slots than manufacturing slots is a typo, not
    extra capacity."""
    from app.manufacturing.schedule import SlotLimits

    assert SlotLimits(manufacturing=10, capital=25).capital_capable() == 10
    assert SlotLimits(manufacturing=0, capital=25).capital_capable() == 25   # pool unlimited


def test_zero_capital_slots_means_unconstrained_not_uncapable():
    """0 reads as "unlimited" for every other slot setting, so it does here too.

    The alternative — 0 meaning "no character can build capitals" — would make
    an unconfigured install report a serialised, absurd capital build instead of
    the same estimate it gave before any of this existed.
    """
    from app.manufacturing.schedule import SlotLimits, schedule_level

    jobs = [_job(100, capital=True) for _ in range(4)]
    assert schedule_level(jobs, SlotLimits(manufacturing=8, capital=0)) == 100
    # A real limit still binds.
    assert schedule_level(jobs, SlotLimits(manufacturing=8, capital=2)) == 200


def test_build_level_plan_splits_then_schedules():
    from app.manufacturing.schedule import SlotLimits, build_level_plan

    specs = [{"type_id": 1, "name": "Reaction", "activity": "reaction",
              "runs": 600, "seconds_per_run": 1440}]     # 600 runs = 10 days
    plan = build_level_plan(1, specs, SlotLimits(reaction=5), max_days=1.0)

    assert plan.job_count == 10                     # ten 1-day jobs
    assert all(j.runs == 60 for j in plan.jobs)
    assert plan.longest_days == pytest.approx(1.0)
    # Ten 1-day jobs across five slots is two days, not one and not ten.
    assert plan.makespan_seconds == pytest.approx(2 * 86400)


def test_capital_components_are_classified_from_the_sde_group():
    from app.manufacturing.schedule import is_capital_component

    assert is_capital_component(873) is True     # Capital Construction Components
    assert is_capital_component(913) is True     # Advanced Capital
    assert is_capital_component(334) is False    # ordinary Construction Components
    assert is_capital_component(None) is False


# ── the two views must describe the same build ───────────────────────────────
def _plan(client, app_module, **defaults):
    """Runs /plan and returns its view model, with defaults applied first."""
    import app.web.main as m

    client.post("/api/settings/defaults", json=defaults)
    captured = {}
    original = m._tr

    def spy(name, request, context):
        captured["view"] = context
        return original(name, request, context)

    m._tr = spy
    try:
        client.post("/plan", data={"product": "Wasp II", "qty": "5000",
                                   "station": "60003760", "mode": "full",
                                   "runs_per_job": "0", "form_me": "7"})
    finally:
        m._tr = original
    return captured["view"].get("result") or {}


def _jobs(result):
    return [j for s in result.get("manufacturing_steps", []) for j in s["jobs"]]


def test_splitting_reaches_both_the_job_list_and_the_materials(client, app_module):
    """The failure this two-pass exists to prevent: materials costed as ten
    jobs while the Jobs tab still shows one.

    Both views are separate BOM resolutions, so both must receive the splits.
    """
    MORPHITE = 11399

    off = _plan(client, app_module, max_job_days=0)
    split = _plan(client, app_module, max_job_days=1.0)

    def morphite(result):
        return next(x["required"] for x in result["materials"] if x["type_id"] == MORPHITE)

    # Smaller jobs cost more materials…
    assert morphite(split) > morphite(off)
    # …and the job list reflects the same split, not one giant job.
    assert len(_jobs(split)) > len(_jobs(off))
    assert max(j["job_duration_seconds"] for j in _jobs(split)) <= 86400 + 1

    restored = _plan(client, app_module, max_job_days=0)
    assert morphite(restored) == morphite(off)
    assert len(_jobs(restored)) == len(_jobs(off))


def test_slot_limits_lengthen_a_split_build(client, app_module):
    """Splitting alone shortens the estimate (each job is shorter); slots are
    what make it realistic again."""
    unlimited = _plan(client, app_module, max_job_days=1.0,
                      manufacturing_slots=0, reaction_slots=0)
    limited = _plan(client, app_module, max_job_days=1.0,
                    manufacturing_slots=10, reaction_slots=10)

    assert len(_jobs(limited)) == len(_jobs(unlimited))     # same work…
    assert limited["fees"]["total_time_s"] > unlimited["fees"]["total_time_s"]
    _plan(client, app_module, max_job_days=0, manufacturing_slots=0, reaction_slots=0)


# ── grouping for the Jobs to Run panel ───────────────────────────────────────
def test_jobs_are_classified_by_sde_group_not_name():
    from app.manufacturing.schedule import classify_job

    assert classify_job(428) == "intermediate"   # Intermediate Materials
    assert classify_job(429) == "composite"      # Composite
    assert classify_job(712) == "biochem"        # Biochemical Material
    assert classify_job(974) == "hybrid"         # Hybrid Polymers
    assert classify_job(334) == "advanced"       # Construction Components
    assert classify_job(873) == "capital"        # Capital Construction Components
    assert classify_job(99999) == "other"
    assert classify_job(None) == "other"


def test_the_end_product_outranks_its_own_group():
    """A capital ship's final assembly belongs under End Product Jobs, not
    filed with the components feeding it."""
    from app.manufacturing.schedule import classify_job

    assert classify_job(873, is_end_product=True) == "end"
    assert classify_job(873, is_end_product=False) == "capital"


def _plan_job(type_id, name, runs, seconds, fee=0.0):
    return {"type_id": type_id, "name": name, "runs": runs,
            "job_duration_seconds": seconds, "job_fee": fee}


def test_group_jobs_summarises_and_orders_categories():
    from app.manufacturing.schedule import group_jobs

    jobs = [
        _plan_job(1, "Carbon Fiber", 25, 86400),
        _plan_job(2, "Ship", 1, 43200),
        _plan_job(3, "Cap Part", 5, 172800),
    ]
    groups = group_jobs(jobs, {1: 428, 2: 540, 3: 873}, end_product_id=2)

    assert [g["key"] for g in groups] == ["intermediate", "capital", "end"]
    assert groups[0]["job_count"] == 1
    assert groups[1]["longest_days"] == 2.0
    # Empty categories are omitted rather than rendered as empty sections.
    assert all(g["job_count"] > 0 for g in groups)


def test_compact_merges_identical_jobs_with_a_count():
    """Fifty identical reactions are one line item and fifty installs."""
    from app.manufacturing.schedule import group_jobs

    jobs = [_plan_job(1, "Carbon Fiber", 25, 86400) for _ in range(5)]
    jobs += [_plan_job(1, "Carbon Fiber", 24, 82944) for _ in range(3)]
    jobs += [_plan_job(2, "Oxy-Organic", 20, 69120)]
    groups = group_jobs(jobs, {1: 428, 2: 428})

    full, compact = groups[0]["jobs"], groups[0]["compact"]
    assert len(full) == 9                       # every install still listed
    assert len(compact) == 3                    # …merged into three line items
    assert {r["job_count"] for r in compact} == {5, 3, 1}
    # Merging must not invent or lose work.
    assert sum(r["job_count"] for r in compact) == len(full)


def test_jobs_to_run_panel_renders(client, app_module):
    """The grouped panel reaches the page, with a compact toggle."""
    result = _plan(client, app_module, max_job_days=1.0,
                   manufacturing_slots=10, reaction_slots=10)
    groups = result.get("job_groups") or []

    assert groups, "a split build must produce grouped jobs"
    assert result["total_job_count"] == sum(g["job_count"] for g in groups)
    # Every job in the plan lands in exactly one category.
    assert result["total_job_count"] == len(_jobs(result))
    _plan(client, app_module, max_job_days=0, manufacturing_slots=0, reaction_slots=0)
