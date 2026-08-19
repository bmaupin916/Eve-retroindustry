# Querying blueprint data from the SDE

Full column-level schema: [eve-sde's columns.md](../../eve-sde/SKILL.md)
(search for the `blueprints` module). This file just covers the industry-
specific gotcha (blueprint typeID ≠ item typeID) and the `activityID`
mapping, with a verified worked example.

## Blueprint typeID is not the item's typeID

Every buildable item has a **separate blueprint type**, e.g. typeID 587 is
"Rifter" (the ship) but typeID 691 is "Rifter Blueprint" — a different
`invTypes` row entirely. `industryActivity*` tables are keyed on the
**blueprint's** typeID, not the product's. Resolve the blueprint's own
typeID by name (or via `industryActivityProducts.productTypeID` if you
already have the product's typeID and want to work backwards) before
querying — don't assume the ship/module's typeID also indexes its blueprint.

## `activityID` mapping

From the jsonl-evesde loader's own source
(`tableloader/tableFunctions/blueprints.py`), so this is exact, not
approximate. This project stores the same activities as the string names
`manufacturing` / `reaction` / `invention` in
`sde_blueprint_products.activity`, not as numeric ids:

| activityID | Activity |
|---|---|
| 1 | manufacturing |
| 3 | research_time (TE research) |
| 4 | research_material (ME research) |
| 5 | copying |
| 8 | invention |
| 11 | reaction |

## Tables

- `industryActivity` — `typeID` (blueprint), `activityID`, `time` (seconds,
  base/unbonused, per run for that activity).
- `industryActivityMaterials` — `typeID`, `activityID`, `materialTypeID`,
  `quantity` (per run, base/unbonused).
- `industryActivityProducts` — `typeID`, `activityID`, `productTypeID`,
  `quantity` (output per run — usually 1, but check; e.g. ammo blueprints
  output in stacks).
- `industryActivityProbabilities` — `typeID`, `activityID`, `productTypeID`,
  `probability` — invention success chance (activityID 8 rows only).
- `industryActivitySkills` — `typeID`, `activityID`, `skillID`, `level` —
  skill requirements per activity.
- `industryBlueprints` — `typeID`, `maxProductionLimit` — the max run count
  allowed in a single manufacturing job for that blueprint.

## Worked example: Rifter Blueprint manufacturing (verified against `eve.db`)

**Table names below are the classic SDE ones.** This project renames them —
`industryActivityMaterials` is `sde_blueprint_materials` here, and there is no
`invTypes`. See the [eve-sde skill](../../eve-sde/SKILL.md) for the full
mapping before running any of this against `sde_base.db`.

```sql
-- confirm the blueprint's own typeID (don't reuse the ship's typeID, 587)
SELECT typeID, typeName FROM invTypes WHERE typeName = 'Rifter Blueprint';
-- => 691

SELECT mt.typeName AS material, m.quantity
FROM industryActivityMaterials m
JOIN invTypes mt ON mt.typeID = m.materialTypeID
WHERE m.typeID = 691 AND m.activityID = 1;
-- Tritanium 32000 / Pyerite 6000 / Mexallon 2500 / Isogen 500

SELECT time FROM industryActivity WHERE typeID = 691 AND activityID = 1;
-- 6000 (seconds, base, before Industry/Advanced Industry/structure bonuses)

SELECT pt.typeName, p.quantity
FROM industryActivityProducts p
JOIN invTypes pt ON pt.typeID = p.productTypeID
WHERE p.typeID = 691 AND p.activityID = 1;
-- Rifter, 1
```

These are the base (ME0, no bonuses) numbers — plug the material total into
the [job cost formula](job-cost.md) as "estimated item value" input, and
apply ME/skill/structure bonuses per [me-te.md](me-te.md) before treating
either number as what a real job will actually consume/take.

## Facility/structure bonus tables (new since the loader's per-file split)

The eve-sde loader restructuring added several industry-adjacent tables.
Only one of them matters for modern player industry math — the rest are
**legacy/vestigial systems** left over from the old NPC-station and
POS-assembly-line era. Don't cross-reference them as if they describe
today's Upwell structures:

- **`indModifierSources`** — the table that actually matters. Maps a
  structure typeID → `(activityName, modifierType, dogmaAttributeID)`,
  telling you which dogma attribute on that structure drives its
  material/time/cost bonus for each activity. See
  [me-te.md](me-te.md#looking-up-exact-bonus-numbers-sde-verified-dont-guess)
  for the exact attribute IDs and worked numbers.
- **`indTargetFilters`/`indTargetFilterCategories`/`indTargetFilterGroups`** —
  catalog of named scopes (`Small T1 Ships`, `Capital Components`, `Hybrid
  Reactions`, ...) used to describe what a rig bonus applies to, resolved to
  `invCategories`/`invGroups` IDs. Also current/relevant.
- **`ramActivities`/`ramAssemblyLines`/`ramAssemblyLineTypeDetailPerGroup`/
  `ramInstallationTypeContents`** — the **old NPC-station and POS
  assembly-line system**. Verified against `eve.db`: modern Upwell
  structures (Raitaru, Azbel, Sotiyo, ...) do **not** appear in
  `ramInstallationTypeContents` at all — only old outpost/array typeIDs do
  (Equipment Assembly Array, Research Laboratory, Caldari Research Outpost,
  ...). Don't use these tables for current player structure math; use
  `indModifierSources` instead.
  - **Gotcha**: `ramActivities.activityID` is a *different ID space* from
    `industryActivity.activityID` despite the shared column name — in
    `ramActivities`, `9` = "Reactions". In `industryActivity` (the one that
    matters — see the `activityID` mapping above), reactions are `11`, and
    `9` doesn't appear at all. Never assume these two `activityID` columns
    are interchangeable.
- **`staOperations`/`staOperationTypes`/`staOperationServices`/
  `staStandingsRestrictions`** — NPC station *economic flavor* data (which
  simulated "sector" — agriculture, mining, manufacturing, ... — an NPC
  station belongs to, used for backstory/name generation). Not part of the
  player industry system at all; safe to ignore for job-cost or production
  questions.
