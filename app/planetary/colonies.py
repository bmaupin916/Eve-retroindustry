"""Colony sizing model — how many colonies of what, for a given demand rate.

Everything here is a **player assumption**, not SDE data: command centre
capacity, structure costs, the standard layouts and the extraction derate are
all game constants or planning conventions that live nowhere in the local DB.
They are module constants so a route can override them from query params.

The one thing this module does *not* hardcode is production rates — those come
from `PIResolver.facility_rate_per_day()`, i.e. from SDE cycle times. A colony
is a count of facilities; how much a facility makes is the schematic's
business.

The load-bearing idea is **net external demand** (see `external_demand`). Total
demand by tier is not a colony plan: a P2 extraction colony refines its own P1
feedstock on-planet, so that P1 must not also get its own colony. A factory
colony imports everything, so its inputs must. Getting this wrong roughly
triples the colony count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from app.planetary.planet_data import P4_PLANET_TYPES, planets_for_p1, single_planet_types
from app.planetary.schematics import PINode, PIResolver

# ── structures ───────────────────────────────────────────────────────────────

ECU = "Extractor Control Unit"
EXTRACTOR_HEAD = "Extractor Head"
BASIC_INDUSTRY = "Basic Industry Facility"
ADVANCED_INDUSTRY = "Advanced Industry Facility"
HIGH_TECH_PLANT = "High-Tech Production Plant"
STORAGE = "Storage Facility"
LAUNCHPAD = "Launchpad"

# structure → (CPU tf, powergrid MW)
STRUCTURE_COST: dict[str, tuple[int, int]] = {
    ECU:               (400, 2600),
    EXTRACTOR_HEAD:    (110, 550),
    BASIC_INDUSTRY:    (200, 800),
    ADVANCED_INDUSTRY: (500, 700),
    HIGH_TECH_PLANT:   (1100, 400),
    STORAGE:           (500, 700),
    LAUNCHPAD:         (3600, 700),
}

# command centre upgrade level → (CPU tf, powergrid MW)
CCU_CAPACITY: dict[int, tuple[int, int]] = {
    0: (1675, 6000),
    1: (7057, 9000),
    2: (12136, 12000),
    3: (17215, 15000),
    4: (21315, 17000),
    5: (25415, 19000),
}

LINK_CPU_BASE, LINK_CPU_PER_KM = 15.0, 0.20
LINK_PG_BASE, LINK_PG_PER_KM = 10.0, 0.15

DEFAULT_CCU_LEVEL = 5
DEFAULT_FACTORY_CCU_LEVEL = 2

# Theoretical extraction assumes heads sustain their opening rate, which decays
# over the life of a program. 0.60 is the conservative end of the 60–70 % that
# real colonies sustain. Extraction colonies only — a factory colony runs on
# imports and is not extraction-limited.
DEFAULT_DERATE = 0.60

# Which facility produces each tier. Drives "how many of this colony's
# facilities make the thing we're counting" — a P2 colony's 6 BIF make
# feedstock, only its 3 AIF make the P2.
OUTPUT_FACILITY_BY_TIER: dict[int, str] = {
    1: BASIC_INDUSTRY,
    2: ADVANCED_INDUSTRY,
    3: ADVANCED_INDUSTRY,
    4: HIGH_TECH_PLANT,
}


def link_cost(distance_km: float) -> tuple[float, float]:
    """(CPU, PG) for one link of the given length.

    Provided for completeness — link budget is out of scope (§9), and the
    archetypes below simply reserve their spare powergrid for it.
    """
    return (LINK_CPU_BASE + LINK_CPU_PER_KM * distance_km,
            LINK_PG_BASE + LINK_PG_PER_KM * distance_km)


# ── layouts ──────────────────────────────────────────────────────────────────

@dataclass
class ColonyLayout:
    """A structure list on one planet, plus the command centre powering it."""
    name: str
    ccu_level: int
    structures: dict[str, int] = field(default_factory=dict)
    is_extraction: bool = True

    def cpu(self) -> int:
        return sum(STRUCTURE_COST[s][0] * n for s, n in self.structures.items())

    def powergrid(self) -> int:
        return sum(STRUCTURE_COST[s][1] * n for s, n in self.structures.items())

    def capacity(self) -> tuple[int, int]:
        return CCU_CAPACITY[self.ccu_level]

    def spare(self) -> tuple[int, int]:
        cpu_cap, pg_cap = self.capacity()
        return (cpu_cap - self.cpu(), pg_cap - self.powergrid())

    def fits(self) -> bool:
        cpu_spare, pg_spare = self.spare()
        return cpu_spare >= 0 and pg_spare >= 0

    def utilisation(self) -> tuple[float, float]:
        cpu_cap, pg_cap = self.capacity()
        return (self.cpu() / cpu_cap, self.powergrid() / pg_cap)

    def binding_resource(self) -> str:
        """Whichever of CPU/PG is closer to exhausted.

        §5 asserts powergrid binds every extraction layout — worth computing
        rather than trusting, because it is *not* universally true: a
        High-Tech Production Plant is CPU-heavy and PG-light, so a P4 factory
        colony binds on CPU instead.
        """
        cpu_used, pg_used = self.utilisation()
        return "PG" if pg_used >= cpu_used else "CPU"

    def output_facilities(self, tier: int) -> int:
        """How many facilities in this layout produce the given tier."""
        return self.structures.get(OUTPUT_FACILITY_BY_TIER.get(tier, ""), 0)


# The standard layouts. Structure counts are conventions, not derivations: each
# deliberately leaves powergrid spare for links (the P2 layout keeps ~1,100 MW),
# which is why the P1 colony carries 7 BIF and not the 8 that bare capacity
# would allow.
P2_EXTRACTION = ColonyLayout(
    name="P2 extraction",
    ccu_level=5,
    structures={ECU: 2, EXTRACTOR_HEAD: 8, BASIC_INDUSTRY: 6,
                ADVANCED_INDUSTRY: 3, STORAGE: 1, LAUNCHPAD: 1},
)

P1_EXTRACTION = ColonyLayout(
    name="P1 extraction",
    ccu_level=5,
    structures={ECU: 2, EXTRACTOR_HEAD: 10, BASIC_INDUSTRY: 7,
                STORAGE: 1, LAUNCHPAD: 1},
)

# Fixed overhead of a factory colony — no extractors, so the rest of the
# command centre goes to production facilities.
FACTORY_FIXED: dict[str, int] = {LAUNCHPAD: 1, STORAGE: 2}


def max_facilities(facility: str, ccu_level: int = DEFAULT_FACTORY_CCU_LEVEL) -> int:
    """How many of one facility type fit in a factory colony at this CCU level.

    Computed from capacity rather than quoted: AIF come out PG-bound (14 at
    CCU II) and HTPP CPU-bound (6), so a single hardcoded N would be wrong for
    one of them. Ignores link cost, per §9.
    """
    cpu_cap, pg_cap = CCU_CAPACITY[ccu_level]
    cpu_fixed = sum(STRUCTURE_COST[s][0] * n for s, n in FACTORY_FIXED.items())
    pg_fixed = sum(STRUCTURE_COST[s][1] * n for s, n in FACTORY_FIXED.items())
    cpu_each, pg_each = STRUCTURE_COST[facility]
    return max(0, min((cpu_cap - cpu_fixed) // cpu_each, (pg_cap - pg_fixed) // pg_each))


def factory_layout(facility: str, count: int,
                   ccu_level: int = DEFAULT_FACTORY_CCU_LEVEL) -> ColonyLayout:
    """A factory colony holding `count` production facilities."""
    return ColonyLayout(
        name="Factory",
        ccu_level=ccu_level,
        structures={**FACTORY_FIXED, facility: count},
        is_extraction=False,
    )


# ── output ───────────────────────────────────────────────────────────────────

def theoretical_output_per_day(layout: ColonyLayout, tier: int,
                               rate_per_facility_per_day: float) -> float:
    """Units/day at 100 % extraction — facility count × the SDE-derived rate."""
    return layout.output_facilities(tier) * rate_per_facility_per_day


def effective_output_per_day(layout: ColonyLayout, tier: int,
                             rate_per_facility_per_day: float,
                             derate: float = DEFAULT_DERATE) -> float:
    """Sustained units/day. The derate applies to extraction colonies only."""
    theoretical = theoretical_output_per_day(layout, tier, rate_per_facility_per_day)
    return theoretical * derate if layout.is_extraction else theoretical


# ── siting ───────────────────────────────────────────────────────────────────

def colony_kind(resolver: PIResolver, type_id: int) -> str:
    """"extraction" or "factory" for the colony that would make this product.

    P1 and most P2 come off a self-contained extraction colony. A P2 whose two
    P1 inputs share no planet type cannot — it must import P1 and run as a
    factory. P3 and P4 are always factories.
    """
    tier = resolver.tier_of(type_id)
    if tier >= 3:
        return "factory"
    if tier == 2:
        name = resolver.get_type_name(type_id)
        return "extraction" if single_planet_types(name, resolver) else "factory"
    return "extraction"


def candidate_planets(resolver: PIResolver, type_id: int) -> list[str]:
    """Planet types this product can be produced on."""
    tier = resolver.tier_of(type_id)
    name = resolver.get_type_name(type_id)
    if tier == 1:
        return sorted(planets_for_p1(name))
    if tier == 2:
        return single_planet_types(name, resolver)
    if tier == 4:
        # Only the High-Tech Production Plant carries a planet restriction.
        return list(P4_PLANET_TYPES)
    return []          # a P3 factory colony can sit on any planet type


# ── the plan ─────────────────────────────────────────────────────────────────

def external_demand(resolver: PIResolver, roots: list[PINode]) -> dict[int, float]:
    """Per-day demand that has to be *supplied* by a colony, per product.

    Not the same as total demand by tier. Walking down from each root:

    * an **extraction** colony refines its own feedstock on-planet, so the
      walk stops — its P1 and P0 never become colonies of their own
    * a **factory** colony imports everything, so the walk continues into its
      inputs and they accumulate external demand

    For a fuel block this yields exactly the products in §8's colony plan:
    Robotics is a factory product, so its Mechanical Parts and Consumer
    Electronics are imported and counted, while the Water and Electrolytes
    feeding the Coolant colonies are internal and are not.
    """
    demand: dict[int, float] = {}

    def visit(node: PINode) -> None:
        if node.is_raw:
            return          # P0 comes out of the ground, not a colony
        demand[node.type_id] = demand.get(node.type_id, 0.0) + node.quantity
        if colony_kind(resolver, node.type_id) == "factory":
            for child in node.children:
                visit(child)

    for root in roots:
        visit(root)
    return demand


@dataclass
class ColonyRequirement:
    type_id: int
    name: str
    tier: int
    demand_per_day: float
    kind: str                       # "extraction" | "factory"
    layout: ColonyLayout
    output_per_colony: float        # units/day, derate already applied
    colonies: int
    facilities: int                 # production facilities across those colonies
    planet_types: list[str]
    factory_only: bool              # a P2 that cannot be made on one planet
    planet_restricted: bool         # P4 — Barren/Temperate only


def size_colony(resolver: PIResolver, type_id: int, demand_per_day: float,
                derate: float = DEFAULT_DERATE,
                ccu_level: int = DEFAULT_CCU_LEVEL) -> ColonyRequirement:
    """Sizes the colonies for one product at a sustained daily rate."""
    tier = resolver.tier_of(type_id)
    kind = colony_kind(resolver, type_id)
    rate = resolver.facility_rate_per_day(type_id)

    if kind == "extraction":
        layout = P1_EXTRACTION if tier == 1 else P2_EXTRACTION
        if ccu_level != layout.ccu_level:
            # The archetypes are defined at CCU V; a lower command centre
            # cannot power them. Report the shortfall rather than silently
            # inventing a smaller layout.
            layout = ColonyLayout(layout.name, ccu_level, dict(layout.structures))
        per_colony = effective_output_per_day(layout, tier, rate, derate)
        colonies = ceil(demand_per_day / per_colony) if per_colony > 0 else 0
        facilities = colonies * layout.output_facilities(tier)
    else:
        facility = OUTPUT_FACILITY_BY_TIER[tier]
        per_facility = rate                       # no derate: runs on imports
        facilities = ceil(demand_per_day / per_facility) if per_facility > 0 else 0
        cap = max_facilities(facility, DEFAULT_FACTORY_CCU_LEVEL)
        colonies = ceil(facilities / cap) if cap else 0
        layout = factory_layout(facility, min(facilities, cap) if facilities else 0)
        per_colony = per_facility * cap

    return ColonyRequirement(
        type_id=type_id,
        name=resolver.get_type_name(type_id),
        tier=tier,
        demand_per_day=demand_per_day,
        kind=kind,
        layout=layout,
        output_per_colony=per_colony,
        colonies=colonies,
        facilities=facilities,
        planet_types=candidate_planets(resolver, type_id),
        factory_only=(tier == 2 and kind == "factory"),
        planet_restricted=(tier == 4),
    )


def plan_colonies(resolver: PIResolver, roots: list[PINode],
                  derate: float = DEFAULT_DERATE,
                  ccu_level: int = DEFAULT_CCU_LEVEL) -> list[ColonyRequirement]:
    """The colony plan for a set of resolved PI demand trees.

    Ordered highest tier first, which is how the plan reads: the P4/P3 factory
    colonies at the top, the extraction colonies feeding them below.
    """
    demand = external_demand(resolver, roots)
    rows = [size_colony(resolver, tid, qty, derate, ccu_level)
            for tid, qty in demand.items()]
    rows.sort(key=lambda r: (-r.tier, -r.demand_per_day))
    return rows
