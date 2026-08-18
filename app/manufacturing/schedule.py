"""Job splitting and slot-constrained scheduling — realistic build times.

Two problems with treating a build as "one job per product":

* **A single job is not how anyone actually builds.** 600 runs of a reaction is
  one 10-day job on paper; in practice you run it as ten 1-day jobs so the
  output arrives steadily and a mistake costs a day, not a fortnight. Splitting
  also *changes the materials* — EVE rounds ME once per job — so the split has
  to be decided here and fed back into the bill of materials.

* **Slots are finite.** The old model gave each level the duration of its
  longest job, which silently assumed unlimited parallel slots. A hundred
  reactions across ten slots takes ten times longer than the longest one.

### The slot model

Reactions have their own pool. Manufacturing has one pool, and capital-capable
slots are a **subset of it** — two characters with 10 manufacturing slots each,
one of them capital-enabled, give you 20 manufacturing slots of which at most
10 can run capital components at once. It is not 20 + 10.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, floor

SECONDS_PER_DAY = 86400

# Capital component groups — these are the jobs constrained to capital-capable
# slots. Derived from the SDE group, never from the product name.
CAPITAL_COMPONENT_GROUPS = frozenset({
    873,   # Capital Construction Components
    913,   # Advanced Capital Construction Components
})


# Job categories for the "Jobs to Run" panel, in the order they are worked:
# reactions feed components, components feed the end product. Each entry is
# (key, label, SDE group ids). Classification is by the *output product's*
# group — never by name, so a rename in a patch cannot reclassify a job.
JOB_CATEGORIES: tuple[tuple[str, str, frozenset], ...] = (
    ("intermediate", "Intermediate Composite Reactions", frozenset({428})),
    ("composite",    "Composite Reactions",              frozenset({429})),
    ("biochem",      "Biochem Reactions",                frozenset({712})),
    ("hybrid",       "Hybrid Reactions",                 frozenset({974})),
    ("molecular",    "Molecular-Forged Reactions",       frozenset({4096})),
    ("advanced",     "Advanced Components",              frozenset({334})),
    ("capital",      "Capital Components",               CAPITAL_COMPONENT_GROUPS),
    ("other",        "Others",                           frozenset()),
    ("end",          "End Product Jobs",                 frozenset()),
)

_GROUP_TO_CATEGORY: dict[int, str] = {
    group_id: key
    for key, _label, groups in JOB_CATEGORIES
    for group_id in groups
}


def classify_job(group_id: int | None, is_end_product: bool = False) -> str:
    """Category key for one job. The end product always wins over its group —
    a capital ship's final assembly belongs under End Product Jobs, not with
    the components feeding it."""
    if is_end_product:
        return "end"
    return _GROUP_TO_CATEGORY.get(group_id, "other") if group_id else "other"


@dataclass
class SlotLimits:
    """Concurrent job capacity. 0 means "unlimited", which reproduces the old
    behaviour exactly and keeps an unconfigured install working."""
    manufacturing: int = 0
    reaction: int = 0
    capital: int = 0            # how many manufacturing slots can run capital work

    def capital_capable(self) -> int:
        """Capital slots can never exceed the pool they live in."""
        if not self.manufacturing:
            return self.capital
        return min(self.capital, self.manufacturing)


@dataclass
class Job:
    """One installable job — already split, so `runs` fits the day limit."""
    type_id: int
    name: str
    activity: str               # "manufacturing" | "reaction"
    runs: int
    seconds: int
    is_capital: bool = False

    @property
    def days(self) -> float:
        return self.seconds / SECONDS_PER_DAY


@dataclass
class LevelPlan:
    level: int
    jobs: list[Job] = field(default_factory=list)
    makespan_seconds: int = 0

    @property
    def job_count(self) -> int:
        return len(self.jobs)

    @property
    def longest_days(self) -> float:
        return max((j.days for j in self.jobs), default=0.0)


def max_runs_per_job(seconds_per_run: float, max_days: float) -> int | None:
    """How many runs fit inside the day limit.

    Depends only on the *per-run* time, not the total — which is what lets the
    caller decide split sizes before knowing final run counts, and re-resolve
    the bill of materials against them in one extra pass.

    Returns None for "no limit". Always at least 1: a single run that already
    exceeds the limit still has to be run as one job, because EVE has no way to
    install half a run.
    """
    if not max_days or max_days <= 0 or seconds_per_run <= 0:
        return None
    fits = floor((max_days * SECONDS_PER_DAY) / seconds_per_run)
    return max(1, fits)


def split_runs(total_runs: int, per_job: int | None) -> list[int]:
    """Splits a run count into job-sized pieces, largest first.

    The remainder becomes its own smaller job rather than being spread, which
    is what the industry window actually does when you queue N full copies plus
    what's left.
    """
    if total_runs <= 0:
        return []
    if not per_job or per_job >= total_runs:
        return [total_runs]
    full, remainder = divmod(total_runs, per_job)
    jobs = [per_job] * full
    if remainder:
        jobs.append(remainder)
    return jobs


def is_capital_component(group_id: int | None) -> bool:
    return group_id in CAPITAL_COMPONENT_GROUPS if group_id else False


def schedule_level(jobs: list[Job], limits: SlotLimits) -> int:
    """Wall-clock seconds to run one level's jobs across the available slots.

    Longest-processing-time first onto the earliest-free eligible slot. Capital
    jobs are placed first because they are the constrained class — filling
    capital-capable slots with ordinary work first would inflate the result.

    Machine-eligibility scheduling is NP-hard in general; LPT is the standard
    practical heuristic and lands within a few percent. This is a planning
    estimate, not a promise.
    """
    if not jobs:
        return 0

    reactions = [j for j in jobs if j.activity == "reaction"]
    manufacturing = [j for j in jobs if j.activity != "reaction"]

    reaction_span = _pack(reactions, limits.reaction)
    mfg_span = _pack_manufacturing(manufacturing, limits)

    # The two pools run concurrently, so the level takes the longer of them.
    return max(reaction_span, mfg_span)


def _pack(jobs: list[Job], slots: int) -> int:
    """LPT onto `slots` identical machines. 0 slots = unlimited."""
    if not jobs:
        return 0
    if not slots or slots <= 0:
        return max(j.seconds for j in jobs)
    free = [0] * min(slots, len(jobs))
    for job in sorted(jobs, key=lambda j: -j.seconds):
        earliest = min(range(len(free)), key=lambda i: free[i])
        free[earliest] += job.seconds
    return max(free)


def _pack_manufacturing(jobs: list[Job], limits: SlotLimits) -> int:
    """LPT across the manufacturing pool, honouring the capital subset.

    Capital jobs may only occupy capital-capable slots; ordinary jobs may use
    any slot but are placed afterwards, so they naturally settle onto the
    slots capital work cannot use.
    """
    if not jobs:
        return 0
    total = limits.manufacturing
    if not total or total <= 0:
        # Unlimited pool: only the capital sub-limit can still bind.
        capital = [j for j in jobs if j.is_capital]
        ordinary = [j for j in jobs if not j.is_capital]
        return max(_pack(capital, limits.capital_capable()),
                   max((j.seconds for j in ordinary), default=0))

    capable = limits.capital_capable()
    free = [0] * total
    # Slots 0..capable-1 are the capital-capable ones. capital=0 means
    # "unconstrained", consistent with every other slot setting, so an
    # unconfigured install doesn't report an absurd serialised capital build.
    pool = range(capable) if capable else range(total)
    for job in sorted((j for j in jobs if j.is_capital), key=lambda j: -j.seconds):
        earliest = min(pool, key=lambda i: free[i])
        free[earliest] += job.seconds
    for job in sorted((j for j in jobs if not j.is_capital), key=lambda j: -j.seconds):
        earliest = min(range(total), key=lambda i: free[i])
        free[earliest] += job.seconds
    return max(free)


def group_jobs(jobs: list[dict], group_of: dict[int, int],
               end_product_id: int | None = None) -> list[dict]:
    """Buckets plan jobs into the display categories.

    `jobs` are the plan's job dicts, `group_of` maps type_id → SDE group_id.
    Returns only non-empty categories, in working order, each with the summary
    the collapsed header shows (job count, longest job) plus both a full and a
    compact row list.

    Compact merges jobs that are identical in every respect a builder cares
    about — same product, same runs, same duration — into one row with a count.
    Fifty identical 24-run reactions are one line item and fifty installs, not
    fifty things to read.
    """
    buckets: dict[str, list[dict]] = {}
    for job in jobs:
        key = classify_job(
            group_of.get(job["type_id"]),
            is_end_product=(end_product_id is not None and job["type_id"] == end_product_id),
        )
        buckets.setdefault(key, []).append(job)

    out: list[dict] = []
    for key, label, _groups in JOB_CATEGORIES:
        bucket = buckets.get(key)
        if not bucket:
            continue
        longest = max((j.get("job_duration_seconds") or 0 for j in bucket), default=0)
        out.append({
            "key": key,
            "label": label,
            # `step`, `is_final` and `total_cost` mirror the shape the plan page's
            # step sections already render, so categories can *be* the steps
            # without rewriting that view. Numbered below, once the empty
            # categories have been dropped.
            "step": 0,
            "is_final": key == "end",
            "job_count": len(bucket),
            "longest_seconds": longest,
            "longest_days": round(longest / SECONDS_PER_DAY, 2),
            "total_cost": sum(j["total_price"] for j in bucket if j.get("total_price")) or None,
            "fees": sum(j["job_fee"] for j in bucket if j.get("job_fee")) or 0.0,
            "jobs": sorted(bucket, key=lambda j: (j.get("name", ""), -(j.get("runs") or 0))),
            "compact": _compact(bucket),
        })
    for ordinal, group in enumerate(out, start=1):
        group["step"] = ordinal
    return out


def _compact(jobs: list[dict]) -> list[dict]:
    """Merges identical jobs into one row carrying `job_count`."""
    merged: dict[tuple, dict] = {}
    for job in jobs:
        key = (job["type_id"], job.get("runs"), job.get("job_duration_seconds"))
        row = merged.get(key)
        if row is None:
            merged[key] = {**job, "job_count": 1}
        else:
            row["job_count"] += 1
    return sorted(merged.values(),
                  key=lambda j: (j.get("name", ""), -(j.get("runs") or 0)))


def build_level_plan(level: int, specs: list[dict], limits: SlotLimits,
                     max_days: float) -> LevelPlan:
    """Splits each product's runs by the day limit, then schedules the result.

    `specs` are dicts of {type_id, name, activity, runs, seconds_per_run,
    is_capital} — the per-run time is what drives the split.
    """
    plan = LevelPlan(level=level)
    for spec in specs:
        per_run = spec.get("seconds_per_run") or 0
        per_job = max_runs_per_job(per_run, max_days)
        for runs in split_runs(int(spec.get("runs") or 0), per_job):
            plan.jobs.append(Job(
                type_id=spec["type_id"],
                name=spec.get("name", ""),
                activity=spec.get("activity", "manufacturing"),
                runs=runs,
                seconds=int(round(per_run * runs)),
                is_capital=bool(spec.get("is_capital")),
            ))
    plan.makespan_seconds = schedule_level(plan.jobs, limits)
    return plan
