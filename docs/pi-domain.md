---
name: eve-pi
description: EVE Online planetary industry domain knowledge — the P0–P4 commodity chain, colony mechanics, how PI data sits in the SDE, and the modelling traps that produce wrong numbers. Use when working on PI features, the /planets page, the PI planner, or any code touching sde_planet_schematics or planetary ESI endpoints.
---

# EVE Planetary Industry — domain reference

Written for code that models PI. It explains the mechanics well enough to
know *why* a calculation is shaped the way it is, and flags the places where
plausible-looking code produces wrong numbers.

---

## 1. The shape of the thing

PI is a five-tier refining chain that runs on planets rather than in
stations. Every tier is produced by a **schematic** installed in a facility.

| Tier | Name | Made from | Facility | Cycle | Yield |
|---|---|---|---|---|---|
| P0 | Raw resources | extracted from the planet | Extractor Control Unit | continuous | varies |
| P1 | Processed materials | 3,000 × one P0 | Basic Industry Facility | 30 min | 20 |
| P2 | Refined commodities | 40 + 40 of two P1 | Advanced Industry Facility | 1 hr | 5 |
| P3 | Specialised commodities | 10 each of two or three P2 | Advanced Industry Facility | 1 hr | 3 |
| P4 | Advanced commodities | 6 each of three P3 (or 2 P3 + 40 P1) | High-Tech Production Plant | 1 hr | 1 |

Steady-state throughput per facility follows directly: **40 P1/hr, 5 P2/hr,
3 P3/hr, 1 P4/hr**. Those four numbers are the backbone of any capacity
model, and they should be *computed from SDE cycle times*, never typed in.
If your code derives something else, the cycle maths is wrong.

The chain compresses volume hard at the bottom and barely at all at the top:
P0→P1 is a 4× reduction, P1→P2 another 4×, P2→P3 only 1.7–2.5×, and P3→P4
roughly break-even. This is why "refine to at least P1 before hauling" is
universal advice and why P4 is hauled as-is.

---

## 2. Terminology, precisely

- **P0 / R0** — raw resources. Used interchangeably in the community. There
  are 15 of them and each maps to exactly one P1.
- **Colony** — one command centre plus its structures on one planet. A
  character may have one colony per planet, up to 6 planets total
  (Interplanetary Consolidation).
- **Pin** — ESI's word for any structure in a colony. `factory_details`
  carries `schematic_id`; `extractor_details` carries the heads and
  `expiry_time`.
- **Schematic** — a PI recipe. *Not* a blueprint. It has no ME/TE, no
  research, no copies, and is not owned — it is simply selected in a
  facility.
- **Route** — a configured flow of one commodity along links between two
  structures. Links are the roads; routes are the traffic on them.

---

## 3. The traps

These are the mistakes that produce confident, wrong output.

### Planetary schematics have no material efficiency

Blueprint ME does not touch PI. If a job's blueprint is ME 10, that reduces
the *blueprint's* inputs — including its PI inputs — but the cost of
producing those PI commodities on-planet is unchanged. Robotics always costs
10 Mechanical Parts + 10 Consumer Electronics per 3 units, at every ME, in
every structure, forever.

Applying an ME multiplier uniformly down a mixed manufacturing + PI tree is
the single most common modelling error.

### Higher tiers hide feedstock demand

A recipe that lists "Robotics ×1" understates its true P2 draw. Robotics is
itself made of Mechanical Parts and Consumer Electronics, so:

- **Mechanical Parts demand is higher than the recipe suggests** wherever
  Robotics is also consumed
- **Consumer Electronics appears in the plan despite appearing in no recipe**

Any planner must expand P3 and P4 into their P2 feedstock before counting
colonies. Counting only the literal recipe line items undercounts badly.

### Not every P2 can be made on one planet

A P2 needs two P1s, which need two P0s. If no single planet type carries both
P0s, that P2 cannot be produced by a self-contained colony — it needs a
factory colony importing P1 from elsewhere.

The three that fail this test are **Silicate Glass** (Lava + Gas),
**Microfiber Shielding** (Lava + Temperate) and **Polyaramids** (Temperate +
Gas). They make excellent test fixtures precisely because they are the
exception.

This also means "cheaper chain" is ambiguous: a chain with fewer distinct P2
inputs may still be operationally harder if some of those inputs are
cross-planet.

### Theoretical output is not achievable output

A colony's ceiling assumes extractors sustain their peak rate. They don't —
extraction decays over the life of a program, and longer programs trade
per-cycle yield for duration. Real sustained output lands around **60–70% of
theoretical**. Any capacity model needs a derate input, applied to
*extraction* colonies only. Factory colonies run on imported feedstock and
are not extraction-limited.

### Facilities destroy overflow

An industry facility buffers exactly one cycle of input. Anything routed to
it beyond that is deleted, not queued. This is why correct colony layouts
route extractor → storage → facility rather than extractor → facility, and
why storage capacity matters more than it looks. Extraction is bursty; the
buffer absorbs it.

### High-Tech Production Plants are planet-restricted

P4 can only be produced on **Barren or Temperate** planets. Nothing else in
the chain has a planet-type restriction on the *facility* (as opposed to on
the resources).

---

## 4. Colonies are powergrid-bound

A command centre supplies CPU and powergrid; every structure and every link
consumes both. In practice **powergrid runs out long before CPU** on any
extraction colony, which is why standard layouts look the way they do.

| CCU level | CPU (tf) | PG (MW) |
|---|---:|---:|
| 0 | 1,675 | 6,000 |
| 5 | 25,415 | 19,000 |

| Structure | CPU | PG |
|---|---:|---:|
| Extractor Control Unit | 400 | 2,600 |
| Extractor Head | 110 | 550 |
| Basic Industry Facility | 200 | 800 |
| Advanced Industry Facility | 500 | 700 |
| High-Tech Production Plant | 1,100 | 400 |
| Storage Facility | 500 | 700 |
| Launchpad | 3,600 | 700 |

Links cost `10 PG + 0.15/km` and `15 CPU + 0.20/km`. Distance scales with
planet radius, so **Gas planets are expensive to build on** (largest radius)
and **Lava planets are cheap** (smallest). A layout that fits on a Lava
planet may not fit the same structures on a Gas giant.

Worked example — the standard P2 colony at CCU V (2 ECU, 8 heads, 6 BIF,
3 AIF, 1 storage, 1 launchpad): 8,480 CPU and 17,900 PG, leaving ~1,100 PG
for links against 16,900 spare CPU. That 8-head ceiling is a powergrid
consequence, and it is why sustaining theoretical extraction is hard.

---

## 5. Planet types and resources

Eight colonisable types. Each carries five P0 resources. Three resources are
exclusive to one type: **Autotrophs** (Temperate), **Felsic Magma** (Lava),
**Reactive Gas** (Gas).

| Resource | → P1 | Barren | Gas | Ice | Lava | Oceanic | Plasma | Storm | Temperate |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Aqueous Liquids | Water | X | X | X | | X | | X | X |
| Autotrophs | Industrial Fibers | | | | | | | | X |
| Base Metals | Reactive Metals | X | X | | X | | X | X | |
| Carbon Compounds | Biofuels | X | | | | X | | | X |
| Complex Organisms | Proteins | | | | | X | | | X |
| Felsic Magma | Silicon | | | | X | | | | |
| Heavy Metals | Toxic Metals | | | X | X | | X | | |
| Ionic Solutions | Electrolytes | | X | | | | | X | |
| Microorganisms | Bacteria | X | | X | | X | | | X |
| Noble Gas | Oxygen | | X | X | | | | X | |
| Noble Metals | Precious Metals | X | | | | | X | | |
| Non-CS Crystals | Chiral Structures | | | | X | | X | | |
| Planktic Colonies | Biomass | | | X | | X | | | |
| Reactive Gas | Oxidizing Compound | | X | | | | | | |
| Suspended Plasma | Plasmoids | | | | X | | X | X | |

Rarity matters for planning: Plasma is by far the scarcest (~1,500 in the
game, ~250 in highsec) while Gas and Barren are the most common (~19,000
each). A plan that leans on Plasma is harder to site than the colony count
implies.

Each planet type can produce exactly one P3 without imports. Every P4 needs
between two and five planet types.

---

## 6. Extraction

An Extractor Control Unit hosts up to 10 heads, each on one resource. The
program duration slider trades three things at once:

- **Longer program** → larger extraction area, lower per-cycle yield, longer
  cycle time, less babysitting
- **Shorter program** → tight area, high yield, frequent cycles, constant
  attention

Cycle time steps up at duration thresholds (25h → 30 min cycles, 50h → 1 hr,
4d4h → 2 hr, 8d8h → 4 hr). Heads placed too close together interfere and the
UI shows the penalty in red.

Output decays across the program — the headline "units/hr" is the opening
rate, not the average. This is the mechanical reason behind the derate in §3.

---

## 7. Getting goods on and off the planet

Two export paths, one import path:

| | Capacity | Import? | Cost |
|---|---|---|---|
| Command centre launch | 500 m³ | No | 1.5× the tax rate |
| Launchpad → customs office | 10,000 m³ | Yes | 1× export, 0.5× import |

Tax is charged on a **base value per unit** by tier, not on market price:

| Tier | Base value |
|---|---:|
| P0 | 5 ISK |
| P1 | 400 |
| P2 | 7,200 |
| P3 | 60,000 |
| P4 | 1,200,000 |

`Export fee = base × tax rate` (×1.5 via command centre).
`Import fee = base × tax rate × 0.5`.

Highsec customs offices carry a 10% NPC tax on top of the owner's rate,
reducible 1% per level of Customs Code Expertise. Outside highsec there is no
NPC component — player-owned offices charge exactly what the owner sets.

**In sovereign nullsec, Orbital Skyhooks replace customs offices.** They keep
the import/export function and additionally yield colony resources (power,
workforce, reagents) to the sov holder. One per planet, and raidable during
defined vulnerability windows.

---

## 8. Where PI output goes

Useful for reasoning about demand.

- **Structure fuel blocks** — Oxygen (P1), Coolant, Enriched Uranium,
  Mechanical Parts (P2), Robotics (P3). Constant, large-volume demand.
- **T2 components** — Construction Blocks, Consumer Electronics, Mechanical
  Parts, Miniature Electronics, Superconductors, Transmitter, Water-Cooled
  CPU, Robotics, Rocket Fuel.
- **Capital ships** — Capital Core Temperature Regulator needs 20 Integrity
  Response Drones + 20 Self-Harmonizing Power Cores (both P4) + 35 Core
  Temperature Regulators. P4 is the dominant PI sink in capital production.
- **T1.5 components** — Auto-Integrity Preservation Seals (Supertensile
  Plastics + Nanites), Life Support Backup Units (Test Cultures + Viral
  Agent). Their other half comes from R4 moon reactions, not PI.
- **Structures, sov upgrades, POCOs** — all four "big" P4s. A customs office
  gantry upgrade takes 8 each of Broadcast Node, Recursive Computing Module,
  Self-Harmonizing Power Core and Wetware Mainframe.
- **Nanite Repair Paste, boosters, implants** — assorted P2/P3.

---

## 9. How PI sits in this codebase

### Static data

`import_sde.py` parses `planetSchematics.yaml` into:

```sql
sde_planet_schematics(schematic_id, name, cycle_time, output_type_id, output_qty)
sde_planet_schematic_materials(schematic_id, type_id, quantity)
```

That is the **complete** recipe graph — roughly 65 schematics covering P1
through P4, with exact input quantities, output quantity, and cycle time in
seconds. Everything in §1's table is derivable from it. Recipe constants
should never be hardcoded.

What these tables do **not** contain: which planet types yield which
resources (see §5 — hardcode it, it is static), command centre and structure
CPU/PG (see §4), or anything about extraction rates.

### Live data

`app/character/planets.py` wraps two ESI endpoints:

- `GET /characters/{id}/planets/` — colony list
- `GET /characters/{id}/planets/{planet_id}/` — pins, links, routes

Scope `esi-planets.manage_planets.v1`. A 403 means the token predates the
scope and the character needs re-authing — the module returns the string
`"forbidden"` so callers can prompt.

The headline value of the live data is **extractor expiry**: PI is
set-and-forget until a program runs dry, so knowing when to reset is what
matters operationally.

### Graph boundary with manufacturing

`app/bom/resolver.py` treats "no blueprint in the SDE" as a leaf. PI
commodities have no blueprint, so a manufacturing BOM naturally terminates at
them. A PI walker picks up exactly there and continues down
`sde_planet_schematics`. The two graphs meet cleanly; don't try to merge them
into one resolver.

Leaf rule on the PI side is the mirror image: a type with no row in
`sde_planet_schematics.output_type_id` is a raw P0 resource, and recursion
stops.

### Rate maths, not job maths

Manufacturing code rounds per job (`ceil(runs)`, per-job ME rounding). PI
planning is a **rate model over a period** — keep quantities fractional
through the walk and round only for display. Rounding at each tier compounds
error fast on a four-level chain.

---

## 10. Quick sanity checks

If code is producing suspicious numbers, these should all hold:

- A Basic Industry Facility consumes 6,000 P0/hr and emits 40 P1/hr
- Two BIFs exactly feed one AIF running a P2 schematic
- Two P2-producing AIFs exactly feed one P3-producing AIF per input
- A CCU V colony running 6 BIF + 3 AIF outputs 360 P2/day at 100% extraction
- One unit of P4 costs 180 P2 if all three of its P3s take three inputs, or
  120 if they all take two
- Doubling a target quantity doubles every P0 quantity exactly — no ME, no
  rounding drift

---

## Sources

Mechanics: [EVE University wiki — Planetary Industry](https://wiki.eveuniversity.org/Planetary_Industry)
and its sub-pages (Planetary Commodities, Planetary buildings, Planets,
Colony management, Player Owned Customs Office, Orbital Skyhook).

Recipe quantities in §8 verified against blueprint data (Nitrogen Fuel Block
4314, Capital Core Temperature Regulator 57524, Auto-Integrity Preservation
Seal 57515). Schematic quantities come from the SDE at runtime.

Note the wiki's main PI page is flagged as not fully updated for the Equinox
expansion; the Skyhook page is current.
