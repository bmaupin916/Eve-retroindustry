"""Static planet-type ↔ resource matrix — the one PI fact not in the local DB.

`sde_planet_schematics` carries the whole recipe graph, but nothing in the
bundled SDE tables says *which planet types yield which raw resources*. That
mapping has not changed since planetary industry shipped in 2010, so it is
hardcoded here. Type ids are not: resolve them by name against `sde_types`
via `resolve_type_ids()` so the data survives id churn.

The planning question this answers: a P2 needs two P1s, which come from two
P0s. If no single planet type carries both P0s, that P2 cannot be made by a
self-contained extraction colony — it needs a factory colony importing P1
from elsewhere. Silicate Glass, Microfiber Shielding and Polyaramids are the
only three that fail this test.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoids importing the resolver (and sqlite) for callers
    from app.planetary.schematics import PIResolver

PLANET_TYPES: tuple[str, ...] = (
    "Barren", "Gas", "Ice", "Lava", "Oceanic", "Plasma", "Storm", "Temperate",
)

# Each P0 refines into exactly one P1, 3,000 → 20 per cycle.
RESOURCE_TO_P1: dict[str, str] = {
    "Aqueous Liquids":   "Water",
    "Autotrophs":        "Industrial Fibers",
    "Base Metals":       "Reactive Metals",
    "Carbon Compounds":  "Biofuels",
    "Complex Organisms": "Proteins",
    "Felsic Magma":      "Silicon",
    "Heavy Metals":      "Toxic Metals",
    "Ionic Solutions":   "Electrolytes",
    "Microorganisms":    "Bacteria",
    "Noble Gas":         "Oxygen",
    "Noble Metals":      "Precious Metals",
    "Non-CS Crystals":   "Chiral Structures",
    "Planktic Colonies": "Biomass",
    "Reactive Gas":      "Oxidizing Compound",
    "Suspended Plasma":  "Plasmoids",
}

P1_TO_RESOURCE: dict[str, str] = {p1: p0 for p0, p1 in RESOURCE_TO_P1.items()}

# Which planet types carry each raw resource. Every type carries exactly five;
# Autotrophs (Temperate), Felsic Magma (Lava) and Reactive Gas (Gas) are
# exclusive to a single type, which is what makes their P2s hard to site.
RESOURCE_PLANETS: dict[str, frozenset[str]] = {
    "Aqueous Liquids":   frozenset({"Barren", "Gas", "Ice", "Oceanic", "Storm", "Temperate"}),
    "Autotrophs":        frozenset({"Temperate"}),
    "Base Metals":       frozenset({"Barren", "Gas", "Lava", "Plasma", "Storm"}),
    "Carbon Compounds":  frozenset({"Barren", "Oceanic", "Temperate"}),
    "Complex Organisms": frozenset({"Oceanic", "Temperate"}),
    "Felsic Magma":      frozenset({"Lava"}),
    "Heavy Metals":      frozenset({"Ice", "Lava", "Plasma"}),
    "Ionic Solutions":   frozenset({"Gas", "Storm"}),
    "Microorganisms":    frozenset({"Barren", "Ice", "Oceanic", "Temperate"}),
    "Noble Gas":         frozenset({"Gas", "Ice", "Storm"}),
    "Noble Metals":      frozenset({"Barren", "Plasma"}),
    "Non-CS Crystals":   frozenset({"Lava", "Plasma"}),
    "Planktic Colonies": frozenset({"Ice", "Oceanic"}),
    "Reactive Gas":      frozenset({"Gas"}),
    "Suspended Plasma":  frozenset({"Lava", "Plasma", "Storm"}),
}

# High-Tech Production Plants — and therefore all P4 production — are limited
# to these two planet types. Nothing else in the chain restricts the facility.
P4_PLANET_TYPES: tuple[str, ...] = ("Barren", "Temperate")


def planets_with_resource(resource_name: str) -> frozenset[str]:
    """Planet types that yield the given P0."""
    return RESOURCE_PLANETS.get(resource_name, frozenset())


def planets_for_p1(p1_name: str) -> frozenset[str]:
    """Planet types on which the given P1 can be made without imports."""
    resource = P1_TO_RESOURCE.get(p1_name)
    if resource is None:
        return frozenset()
    return planets_with_resource(resource)


def resolve_type_ids(resolver: "PIResolver") -> dict[str, int]:
    """Resolves every P0 and P1 name in this module against `sde_types`.

    Names, not ids, are what is stable enough to hardcode; ids come from the
    DB. A name that does not resolve is left out rather than faked, so a
    caller can tell that the SDE and this table have drifted apart.
    """
    ids: dict[str, int] = {}
    for p0, p1 in RESOURCE_TO_P1.items():
        for name in (p0, p1):
            type_id = resolver.find_type_id(name)
            if type_id is not None:
                ids[name] = type_id
    return ids


def p1_inputs(commodity_name: str, resolver: "PIResolver") -> list[str]:
    """The direct schematic inputs of a commodity, by name (SDE, not hardcoded)."""
    type_id = resolver.find_type_id(commodity_name)
    if type_id is None:
        return []
    row = resolver.find_schematic(type_id)
    if row is None:
        return []
    return [m["name"] for m in resolver.get_materials(row["schematic_id"])]


def single_planet_types(p2_name: str, resolver: "PIResolver") -> list[str]:
    """Planet types where a P2 can be made end-to-end on one colony.

    Both of its P1 inputs must trace to resources natively present on the same
    planet type. An empty list means the P2 needs cross-planet P1 imports and
    must be built on a factory colony — true for exactly Silicate Glass,
    Microfiber Shielding and Polyaramids.

    Returns [] for anything that isn't a P2 (a P3/P4 is a factory product by
    construction, a P1/P0 isn't the question being asked).
    """
    type_id = resolver.find_type_id(p2_name)
    if type_id is None or resolver.tier_of(type_id) != 2:
        return []
    candidates: frozenset[str] | None = None
    for p1 in p1_inputs(p2_name, resolver):
        planets = planets_for_p1(p1)
        candidates = planets if candidates is None else (candidates & planets)
    return sorted(candidates or ())
