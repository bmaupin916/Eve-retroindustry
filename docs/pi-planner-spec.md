# PI Planner — Implementation Spec

Feature brief for adding a **planetary industry planner** to EVE Retroindustry.

Drop this at `docs/pi-planner-spec.md` and hand it to Claude Code.

---

## 1. What this is

The app already has a PI **monitor** (`/planets`): live colonies, extractor
expiry countdowns, factory chains, stored contents. It answers *"what are my
colonies doing right now?"*

This adds a PI **planner**: *"I want N of X per week — how many colonies of
what, on which planet types?"* It works backwards from a target product to
colony counts.

Personal-use feature. Correctness over polish; no need to generalise beyond
what's described here.

---

## 2. Why this is cheap to build

The recipe graph is **already in the local database**. `import_sde.py`
populates:

```sql
sde_planet_schematics(schematic_id, name, cycle_time, output_type_id, output_qty)
sde_planet_schematic_materials(schematic_id, type_id, quantity)
```

That is the complete P1→P4 schematic set with input quantities, output
quantity and cycle time in seconds. **Do not hardcode any PI recipe ratios.**
They all come from these two tables and stay correct through balance patches.

The second gift: `app/bom/resolver.py` already stops exactly where this needs
to start. Its leaf rule is "no blueprint in the SDE", and PI commodities have
no blueprint — so a Fuel Block BOM already terminates at Oxygen, Coolant,
Enriched Uranium, Mechanical Parts and Robotics. The PI planner picks those
leaves up and keeps walking.

---

## 3. Module layout

Follow existing conventions — logic in a package, view model in a
`*_helper.py`, the route thin. Since W6 the routes live in
`app/web/routers/`; the PI ones are in `routers/planets.py`.

```
app/planetary/__init__.py
app/planetary/schematics.py     # PI recipe graph walk (pure, SDE-only)
app/planetary/colonies.py       # colony sizing model (player assumptions)
app/planetary/planet_data.py    # static planet-type ↔ resource matrix
app/web/pi_planner_helper.py    # view model assembly
app/web/templates/pi_planner.html
tests/test_pi_planner.py
```

Plus: one route in `app/web/routers/planets.py`, one nav entry in
`app/web/templates/base.html`.

---

## 4. `app/planetary/schematics.py`

### Data model

```python
@dataclass
class PINode:
    type_id: int
    name: str
    tier: int                 # 0 = raw resource, 1..4 = P1..P4
    quantity: float           # units needed per period
    is_raw: bool              # tier == 0
    schematic_id: int | None
    output_qty: int           # units per cycle
    cycle_time: int           # seconds
    children: list["PINode"]
```

### Core class

```python
class PIResolver:
    def __init__(self, db_path: str): ...
    def find_schematic(self, type_id: int) -> sqlite3.Row | None
    def tier_of(self, type_id: int) -> int
    def resolve(self, type_id: int, quantity: float) -> PINode
    def aggregate_by_tier(self, node: PINode) -> dict[int, dict[int, float]]
```

### Rules

**Leaf detection.** `find_schematic()` returns the row from
`sde_planet_schematics` where `output_type_id = ?`. `None` → raw resource
(P0), recursion stops. Same shape as `BOMResolver.find_blueprint`.

**Tier.** Derive, don't hardcode: a type with no schematic is tier 0;
otherwise tier is `1 + max(tier of inputs)`. Memoise — the graph is small
(~65 schematics) but the walk revisits nodes constantly.

**No material efficiency.** Planetary schematics have no ME research. This is
the single most common modelling error. Quantities are exact:

```python
cycles = quantity / row["output_qty"]          # keep fractional
child_qty = cycles * material_row["quantity"]
```

Do **not** apply `ceil()` per level — this is a rate model over a period, not
a discrete job. Round only at presentation.

**Whole units at the edge.** You cannot produce 1,446.43 Coolant, so every
figure shown to the user is ceiled: `whole_units(1446.43) == 1447`. That
happens once, at the boundary of the view model — children always derive from
the exact fractional rate. Ceiling each tier and feeding the rounded number
downwards compounds the error four times over a P4 → P0 chain, which is the
whole reason the walk stays fractional.

**Cycle counting.** Facilities needed for a node:

```python
cycles_per_day = 86400 / cycle_time
units_per_facility_per_day = output_qty * cycles_per_day
facilities = quantity_per_day / units_per_facility_per_day
```

For reference, this yields 5 P2/hr and 3 P3/hr and 1 P4/hr from SDE data
alone — matching in-game values, which is a good smoke test.

**Cycle detection.** Carry a `visited: frozenset[int]` per branch exactly as
`BOMResolver.resolve` does. The PI graph is acyclic today; don't rely on it.

### Composition with the manufacturing BOM

Entry points, in order of usefulness:

1. **From a blueprint product** (Fuel Block, Capital Core Temperature
   Regulator). Run `BOMResolver.resolve()`, take
   `node.aggregate_leaves()`, filter to leaves that *do* have a PI schematic,
   then `PIResolver.resolve()` each one. This is the headline case.
2. **From a PI commodity directly** (e.g. "500 Robotics/day").

**Where ME applies, and where it stops.** ME belongs to the blueprint and
touches only its own material lines. It does not reach anything below them:
Robotics costs 10 Mechanical Parts + 10 Consumer Electronics per 3 units at
every ME, forever. `PIResolver` has no ME parameter at all, which is the
structural way to guarantee this.

Two rules govern the BOM half, and both follow from "rate, not job":

- **Batch the period's runs.** Pass `runs_per_job=None` so ME applies to the
  whole period in one job. At ME 10, 22 Oxygen/run costs 19.8 — rounding per
  1-run job gives 20 and inflates the rate by 1 %.
- **ME cannot reduce a material below 1 unit per run.** EVE computes
  `max(runs, ceil(base × runs × (1 - ME/100)))`, so a base quantity of 1 is
  never reduced at any ME. This is what makes Robotics in a fuel block
  immune to ME (see the worked example in §8).

Fuel blocks are always planned at **ME 10**.

---

## 5. `app/planetary/colonies.py`

Everything here is a **player assumption**, not SDE data. Expose as module
constants that the route can override from query params.

### Command centre capacity

| CCU level | CPU (tf) | PG (MW) |
|---|---:|---:|
| 0 | 1,675 | 6,000 |
| 1 | 7,057 | 9,000 |
| 2 | 12,136 | 12,000 |
| 3 | 17,215 | 15,000 |
| 4 | 21,315 | 17,000 |
| 5 | 25,415 | 19,000 |

### Structure costs

| Structure | CPU | PG |
|---|---:|---:|
| Extractor Control Unit | 400 | 2,600 |
| Extractor Head | 110 | 550 |
| Basic Industry Facility | 200 | 800 |
| Advanced Industry Facility | 500 | 700 |
| High-Tech Production Plant | 1,100 | 400 |
| Storage Facility | 500 | 700 |
| Launchpad | 3,600 | 700 |

Link cost: `PG = 10 + 0.15/km`, `CPU = 15 + 0.20/km`.

### Colony archetypes

Powergrid binds every extraction layout; CPU has large headroom. Verify this
in code rather than trusting the numbers below.

| Archetype | Layout | Theoretical output |
|---|---|---:|
| P2 extraction (CCU V) | 2 ECU, 8 heads, 6 BIF, 3 AIF, 1 storage, 1 launchpad | 360 units/day |
| P1 extraction (CCU V) | 2 ECU, 10 heads, 7 BIF, 1 storage, 1 launchpad | 6,720 units/day |
| Factory (CCU II) | 1 launchpad, N AIF, 2 storage — no extractors | 72 units/day per AIF (P3) |

### Extraction derate

Theoretical P2 output assumes 18,000 units/hr per resource sustained from 4
heads per ECU, which does not hold across a multi-day extraction program.
Default the derate to **0.60**, make it a UI input, and apply it to
extraction colonies only — factory colonies run on imports and are not
extraction-limited.

### Facility restrictions

- High-Tech Production Plant builds **only on Barren or Temperate** planets.
  Flag it in the output when a P4 is in the plan.
- A colony can only make a P2 without imports if **both** its P1 inputs trace
  to resources present on one planet type (see §6).

---

## 6. `app/planetary/planet_data.py`

The one thing not in the local DB: which planet types yield which resources.
Static since 2010 — hardcode it.

```python
RESOURCE_TO_P1 = {
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
```

Resource availability by planet type:

| Resource | Barren | Gas | Ice | Lava | Oceanic | Plasma | Storm | Temperate |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Aqueous Liquids | X | X | X | | X | | X | X |
| Autotrophs | | | | | | | | X |
| Base Metals | X | X | | X | | X | X | |
| Carbon Compounds | X | | | | X | | | X |
| Complex Organisms | | | | | X | | | X |
| Felsic Magma | | | | X | | | | |
| Heavy Metals | | | X | X | | X | | |
| Ionic Solutions | | X | | | | | X | |
| Microorganisms | X | | X | | X | | | X |
| Noble Gas | | X | X | | | | X | |
| Noble Metals | X | | | | | X | | |
| Non-CS Crystals | | | | X | | X | | |
| Planktic Colonies | | | X | | X | | | |
| Reactive Gas | | X | | | | | | |
| Suspended Plasma | | | | X | | X | X | |

Resolve type_ids by name against `sde_types` at import time; don't hardcode
type_ids.

Derive `single_planet_types(p2_name) -> list[str]` from this matrix — the
planet types on which both inputs are natively available. An empty result
means the P2 needs cross-planet P1 imports and must be built on a factory
colony. **Silicate Glass, Microfiber Shielding and Polyaramids are the three
that fail this test** and are a good test fixture.

---

## 7. Route and page

```
GET  /pi-planner
POST /pi-planner   (or GET with query params — match how /plan does it)
```

Inputs:

| Field | Default |
|---|---|
| Target product (type-ahead over PI commodities + blueprint products) | — |
| Quantity | — |
| Period (day / week / month) | week |
| Extraction derate | 0.60 |
| Command centre level | 5 |
| Blueprint ME (when the target is a manufactured item) | 10 |

Output sections:

1. **Manufacturing inputs** — only when the target is a blueprint product.
   The BOM leaves, split into PI vs non-PI (ice, minerals, moon goo). Say
   plainly that non-PI inputs are out of scope.
2. **PI demand by tier** — P4 → P3 → P2 → P1 → P0, with per-period
   quantities.
3. **Colony plan** — per product: demand/day, output/colony, colonies needed,
   candidate planet types, and a flag for factory-only products.
4. **Plan vs actual** — see below.

### Plan vs actual

This is the feature no other PI tool can offer, because nothing else has both
halves. Cross-reference the colony plan against live ESI data from
`app/character/planets.py`:

- Count existing colonies by output product (derive from each colony's
  factory pins → `factory_details.schematic_id` → `sde_planet_schematics`)
- Show `needed / have / gap` per product
- Surface extractors expiring within 24h on colonies that the plan depends on

Reuse whatever the `/planets` route already does for fetching and caching
rather than issuing fresh ESI calls.

---

## 8. Tests — `tests/test_pi_planner.py`

Run against the committed `sde_base.db`; no network, no ESI.

| Test | Assertion |
|---|---|
| `test_tier_derivation` | Oxygen → 1, Coolant → 2, Robotics → 3, Broadcast Node → 4 |
| `test_raw_is_leaf` | Noble Gas has no schematic, `is_raw` is True, tier 0 |
| `test_p2_ratio` | Coolant: 40 Water + 40 Electrolytes → 5 output, 3600s cycle |
| `test_robotics_feedstock` | 1 Robotics needs 10/3 Mechanical Parts and 10/3 Consumer Electronics |
| `test_no_me_applied` | Doubling target quantity exactly doubles every P0 quantity |
| `test_facility_rates` | P1 = 40/hr, P2 = 5/hr, P3 = 3/hr, P4 = 1/hr, computed from SDE |
| `test_fuel_block_handoff` | Nitrogen Fuel Block BOM leaves include exactly the 5 PI commodities; ice products classified non-PI |
| `test_two_planet_p2` | `single_planet_types()` is empty for Silicate Glass, Microfiber Shielding, Polyaramids; non-empty for Coolant |
| `test_cycle_guard` | Deliberate cycle in a fixture terminates |

### Worked example for a golden test

Nitrogen Fuel Block, blueprint ME 10, 50,000 blocks/week, derate 0.60.

Base recipe per run (40 blocks): Oxygen 22, Coolant 9, Enriched Uranium 4,
Mechanical Parts 4, Robotics 1.

1,250 runs = 50,000 blocks/week, so ~178.57 runs/day. Expected demand per
day, exact rate on the left and the whole units shown on the page on the
right:

| Product | Tier | Exact/day | Shown | Derivation |
|---|:-:|---:|---:|---|
| Oxygen | P1 | 3,535.71 | 3,536 | 22 × 0.9 × 178.57 |
| Coolant | P2 | 1,446.43 | 1,447 | 9 × 0.9 |
| Mechanical Parts | P2 | 1,238.10 | 1,239 | 642.86 direct + 595.24 for Robotics |
| Enriched Uranium | P2 | 642.86 | 643 | 4 × 0.9 |
| Consumer Electronics | P2 | 595.24 | 596 | Robotics × 10/3 |
| Robotics | P3 | 178.57 | 179 | 1 × **1.0** — see below |

**Robotics is not reduced by ME.** Its base quantity is 1 per run, and EVE
floors consumption at one unit per run, so `max(1250, ceil(1250 × 0.9))` =
1,250/week. An earlier draft of this example applied 0.9 anyway and got
161/day, which then propagated into Consumer Electronics (536/day) and into
Robotics' share of Mechanical Parts. The four materials with a base quantity
above 1 are ME-reduced normally and were correct as written.

Colony counts at 216 units/day effective (P2) and 4,032/day (P1): Coolant 7,
Mechanical Parts 6, Enriched Uranium 3, Consumer Electronics 3, Oxygen 1,
plus a Robotics factory colony (3 AIF). The correction above does not move
any of these — each affected product lands in the same bucket after ceiling.

---

## 9. Out of scope

- Extractor head placement, deposit richness, planet radius / link costs
- Ice products, moon reactions, minerals — surface them as unhandled BOM
  leaves and stop
- Writing anything back to ESI
- Multi-character colony assignment (show totals; let the user divide)

---

## 10. Notes for the implementer

- Put logic in the modules above; the route should be thin. This spec was
  written when every route lived in a 296 KB `main.py` — W6 has since split
  that into `app/web/routers/`, but the reason for keeping a route thin has
  not changed.
- Mirror `BOMResolver`'s caching approach — memoise schematic lookups, type
  names and tiers on the resolver instance.
- The resolver must be usable headless (constructor takes `db_path`) so tests
  don't need the web app.
- Sanity check while building: the SDE-derived facility rates should come out
  at exactly 5 P2/hr and 3 P3/hr. If they don't, the cycle-time maths is
  wrong.
