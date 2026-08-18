"""Recursive resolver for the planetary-industry recipe graph.

Mirror image of `app/bom/resolver.py`: that one stops at "no blueprint in the
SDE", which is exactly where PI starts (PI commodities have no blueprint).
This one walks `sde_planet_schematics` / `sde_planet_schematic_materials` from
a P4/P3/P2/P1 commodity down to the raw P0 resources and stops at "no
schematic in the SDE".

Two things make the maths different from manufacturing, and both are easy to
get wrong:

* **No material efficiency.** Planetary schematics have no ME research, no
  structure bonus and no rigs. Quantities are exact ratios, always.
* **Rates, not jobs.** This is "how much per day", not "how many runs of a
  job", so quantities stay fractional through the whole walk. Rounding at
  each tier compounds fast over a four-level chain — round only for display.

Nothing here is a player assumption; every number comes from the SDE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
import sqlite3

# Sentinel for "cache miss" — `None` is a valid stored value (a type with no
# schematic is a raw resource), so it can't double as the miss marker.
_MISSING = object()

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400


@dataclass
class PINode:
    type_id: int
    name: str
    tier: int                 # 0 = raw resource (P0), 1..4 = P1..P4
    quantity: float           # units needed per period — fractional on purpose
    is_raw: bool              # True when the type has no schematic (tier 0)
    schematic_id: int | None
    output_qty: int           # units produced per cycle (0 for raw)
    cycle_time: int           # seconds per cycle (0 for raw)
    children: list["PINode"] = field(default_factory=list)

    def walk(self):
        """Yields every node in the tree, this one first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def leaves(self) -> dict[int, tuple[str, float]]:
        """Returns {type_id: (name, total_qty)} for the raw P0 resources."""
        acc: dict[int, tuple[str, float]] = {}
        for node in self.walk():
            if not node.is_raw:
                continue
            prev = acc.get(node.type_id)
            acc[node.type_id] = (node.name, (prev[1] if prev else 0.0) + node.quantity)
        return acc


class PIResolver:
    """Walks the PI schematic graph. Usable headless — constructor takes a path.

    Caches schematic lookups, materials, type names and tiers on the instance
    the way `BOMResolver` does: the graph is small (~68 schematics) but the
    walk revisits the same nodes constantly (Water appears under most P2s).
    """

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._schematic_cache: dict[int, sqlite3.Row | None] = {}
        self._mat_cache: dict[int, list[sqlite3.Row]] = {}
        self._name_cache: dict[int, str] = {}
        self._type_id_cache: dict[str, int | None] = {}
        self._tier_cache: dict[int, int] = {}

    def close(self):
        self.conn.close()

    # ── SDE lookups ──────────────────────────────────────────────────────────

    def get_type_name(self, type_id: int) -> str:
        cached = self._name_cache.get(type_id)
        if cached is not None:
            return cached
        row = self.conn.execute(
            "SELECT name FROM sde_types WHERE type_id=?", (type_id,)
        ).fetchone()
        name = row["name"] if row else f"Unknown ({type_id})"
        self._name_cache[type_id] = name
        return name

    def find_type_id(self, name: str) -> int | None:
        """Resolves a type by exact name. Lets callers hold names, not ids."""
        cached = self._type_id_cache.get(name, _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        row = self.conn.execute(
            "SELECT type_id FROM sde_types WHERE name=?", (name,)
        ).fetchone()
        tid = row["type_id"] if row else None
        self._type_id_cache[name] = tid
        return tid

    def find_schematic(self, type_id: int) -> sqlite3.Row | None:
        """The schematic producing this type, or None → raw P0 resource.

        Same shape as `BOMResolver.find_blueprint`, but without its
        test-blueprint filtering: `output_type_id` is unique across
        `sde_planet_schematics`, so there is nothing to disambiguate.
        """
        cached = self._schematic_cache.get(type_id, _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        row = self.conn.execute("""
            SELECT schematic_id, name, cycle_time, output_type_id, output_qty
            FROM sde_planet_schematics
            WHERE output_type_id = ?
        """, (type_id,)).fetchone()
        self._schematic_cache[type_id] = row
        return row

    def get_materials(self, schematic_id: int) -> list[sqlite3.Row]:
        cached = self._mat_cache.get(schematic_id)
        if cached is not None:
            return cached
        rows = self.conn.execute("""
            SELECT m.type_id, m.quantity, t.name
            FROM sde_planet_schematic_materials m
            JOIN sde_types t ON t.type_id = m.type_id
            WHERE m.schematic_id = ?
        """, (schematic_id,)).fetchall()
        self._mat_cache[schematic_id] = rows
        return rows

    def is_pi_commodity(self, type_id: int) -> bool:
        """True when the type is produced by a schematic (P1..P4)."""
        return self.find_schematic(type_id) is not None

    # ── tier ─────────────────────────────────────────────────────────────────

    def tier_of(self, type_id: int) -> int:
        """0 for a raw resource, otherwise 1 + the deepest input tier.

        Derived, never hardcoded — a balance patch that reshuffles the chain
        stays correct.
        """
        tier, _cycled = self._tier(type_id, frozenset())
        return tier

    def _tier(self, type_id: int, stack: frozenset[int]) -> tuple[int, bool]:
        """Returns (tier, saw_cycle). A tier computed through a cycle is not
        memoised — it would poison the cache for the whole run."""
        cached = self._tier_cache.get(type_id)
        if cached is not None:
            return cached, False
        row = self.find_schematic(type_id)
        if row is None:
            self._tier_cache[type_id] = 0
            return 0, False
        if type_id in stack:
            return 0, True
        below = stack | {type_id}
        best, cycled = 0, False
        for mat in self.get_materials(row["schematic_id"]):
            mat_tier, mat_cycled = self._tier(mat["type_id"], below)
            best = max(best, mat_tier)
            cycled = cycled or mat_cycled
        tier = best + 1
        if not cycled:
            self._tier_cache[type_id] = tier
        return tier, cycled

    # ── the walk ─────────────────────────────────────────────────────────────

    def resolve(
        self,
        type_id: int,
        quantity: float,
        visited: frozenset[int] | None = None,
    ) -> PINode:
        """Expands `quantity` units of `type_id` into its full input tree.

        Quantities are exact and fractional:

            cycles    = quantity / output_qty      # kept fractional
            child_qty = cycles * material_quantity

        No `ceil()` per level — a colony runs continuously, so a demand of
        1.5 cycles/day is a real, meaningful number. `visited` is carried per
        branch (an immutable copy, as `BOMResolver.resolve` does) so a cyclic
        schematic graph terminates. The PI graph is acyclic today; the guard
        costs nothing and doesn't rely on that staying true.
        """
        if visited is None:
            visited = frozenset()

        name = self.get_type_name(type_id)
        row = self.find_schematic(type_id)

        if row is None:
            return PINode(
                type_id=type_id, name=name, tier=0, quantity=quantity,
                is_raw=True, schematic_id=None, output_qty=0, cycle_time=0,
            )

        node = PINode(
            type_id=type_id, name=name, tier=self.tier_of(type_id),
            quantity=quantity, is_raw=False,
            schematic_id=row["schematic_id"],
            output_qty=row["output_qty"], cycle_time=row["cycle_time"],
        )
        # Cycle: keep the node (it is a real commodity with a real schematic)
        # but stop expanding. `is_raw` stays False, so a truncated branch is
        # distinguishable from a genuine P0 leaf.
        if type_id in visited:
            return node

        below = visited | {type_id}
        cycles = quantity / row["output_qty"]
        for mat in self.get_materials(row["schematic_id"]):
            node.children.append(
                self.resolve(mat["type_id"], cycles * mat["quantity"], below)
            )
        return node

    def resolve_many(self, demands: dict[int, float]) -> list[PINode]:
        """Resolves several targets at once — the BOM-handoff entry point."""
        return [self.resolve(tid, qty) for tid, qty in demands.items()]

    def aggregate_by_tier(self, node: PINode) -> dict[int, dict[int, float]]:
        """Total demand per type, grouped by tier: {tier: {type_id: qty}}.

        Sums across branches, which is the whole point: Mechanical Parts
        appear both directly in a fuel-block recipe and again as Robotics
        feedstock, and a colony plan needs the combined figure.
        """
        acc: dict[int, dict[int, float]] = {}
        for n in node.walk():
            acc.setdefault(n.tier, {})
            acc[n.tier][n.type_id] = acc[n.tier].get(n.type_id, 0.0) + n.quantity
        return acc

    def aggregate_many_by_tier(self, nodes: list[PINode]) -> dict[int, dict[int, float]]:
        """`aggregate_by_tier` over several trees, merged."""
        acc: dict[int, dict[int, float]] = {}
        for node in nodes:
            for tier, per_type in self.aggregate_by_tier(node).items():
                dest = acc.setdefault(tier, {})
                for tid, qty in per_type.items():
                    dest[tid] = dest.get(tid, 0.0) + qty
        return acc

    # ── capacity ─────────────────────────────────────────────────────────────

    def facility_rate_per_hour(self, type_id: int) -> float:
        """Units of `type_id` one facility produces per hour, from SDE alone.

        Sanity check: this must come out at 40 P1, 5 P2, 3 P3, 1 P4. Anything
        else means the cycle-time maths is wrong.
        """
        row = self.find_schematic(type_id)
        if row is None or not row["cycle_time"]:
            return 0.0
        return row["output_qty"] * SECONDS_PER_HOUR / row["cycle_time"]

    def facility_rate_per_day(self, type_id: int) -> float:
        row = self.find_schematic(type_id)
        if row is None or not row["cycle_time"]:
            return 0.0
        return row["output_qty"] * SECONDS_PER_DAY / row["cycle_time"]

    def facilities_needed(self, type_id: int, quantity_per_day: float) -> float:
        """Fractional facility count for a sustained daily rate.

        Fractional on purpose — 6.2 facilities is a useful planning number,
        and rounding is a presentation decision.
        """
        per_facility = self.facility_rate_per_day(type_id)
        if not per_facility:
            return 0.0
        return quantity_per_day / per_facility


def whole_units(quantity: float) -> int:
    """Rounds a fractional rate up to whole units — for display only.

    You can't produce 1,446.43 Coolant, so anything shown to a user is
    ceiled: 1,446.43 → 1,447. `round(…, 6)` first so float noise doesn't
    push an exact 4,032.0 up to 4,033.

    Call this at the edge, never inside the walk. Ceiling each tier and
    feeding the rounded figure downwards compounds: over a P4 → P0 chain the
    error stacks four times, and the whole reason `resolve()` keeps
    quantities fractional is to avoid exactly that. Children derive from the
    exact rate; only the number on the page is a whole one.
    """
    return ceil(round(quantity, 6))


def whole_units_by_type(per_type: dict[int, float]) -> dict[int, int]:
    """`whole_units` across one tier of `aggregate_by_tier` output."""
    return {type_id: whole_units(qty) for type_id, qty in per_type.items()}


def split_pi_leaves(
    leaves: dict[int, tuple[str, int]],
    resolver: PIResolver,
) -> tuple[dict[int, tuple[str, float]], dict[int, tuple[str, float]]]:
    """Splits `BOMNode.aggregate_leaves()` output into (PI, non-PI).

    The handoff between the two graphs: a manufacturing BOM bottoms out at
    raw materials, some of which are PI commodities and some of which are ice
    products, minerals or moon goo. The PI half continues down
    `PIResolver.resolve`; the rest is out of scope and gets surfaced as-is.
    """
    pi: dict[int, tuple[str, float]] = {}
    other: dict[int, tuple[str, float]] = {}
    for type_id, (name, qty) in leaves.items():
        dest = pi if resolver.is_pi_commodity(type_id) else other
        dest[type_id] = (name, float(qty))
    return pi, other
