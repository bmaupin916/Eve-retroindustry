# SDE Data Domains

> **Scope note.** This file documents the **full** SDE as loaded by the
> external `jsonl-evesde` toolkit — 176 tables, classic names (`invTypes`,
> `industryActivityMaterials`). **This project ships a 14-table subset under
> different names** (`sde_types`, `sde_blueprint_materials`), so most of what
> follows is not queryable against `sde_base.db` without importing it first.
> See the [parent skill](../SKILL.md) for the name mapping and what is present.
> Absolute paths below belong to the toolkit author's environment.

The SDE isn't one flat table — it's 176 tables across a handful of
conceptual domains. This covers the major ones and how their tables relate;
see the "Other domains" section below for newer, smaller domains not
detailed here.
Column names below are verified against
`/home/scripts/jsonl-evesde/tableloader/tables.py`; see
[table-map.md](table-map.md) for the full JSONL→table list.

## Inventory / Types — the core lookup

Everything that exists as an item, ship, skill, or structure is a row in
**`invTypes`**, keyed by `typeID`. It's the table nearly everything else
references.

```
invCategories (categoryID)
  └─ invGroups (groupID, categoryID)
       └─ invTypes (typeID, groupID, ...)
```

Key `invTypes` columns: `typeName`, `description`, `mass`, `volume`,
`capacity`, `portionSize`, `raceID`, `basePrice`, `published`,
`marketGroupID`, `metaLevel`, `techLevel`.

- `published = false` → not currently usable/visible in-game (draft, removed,
  internal placeholder like typeID 0). Filter these out by default.
- `invMarketGroups` / `marketGroupID` is the market browser tree, separate
  from `invGroups`/`invCategories` (the "what kind of thing is this"
  taxonomy).
- `invMetaTypes` links a variant (e.g. a faction/deadspace module) back to
  its parent type via `parentTypeID`.
- `invTypeMaterials` — reprocessing yield: what raw materials you get back
  from scrapping a `typeID`.

## Dogma — attributes and effects

Dogma is the mechanical-stats system: every gun's damage, every ship's
slots, every module's CPU cost is a **dogma attribute**, and every "does
X when fit/activated" behavior is a **dogma effect**.

- **`dgmAttributeTypes`**: the attribute *definitions* — `attributeID`,
  `attributeName`, `displayName`, `unitID`, `highIsGood`, `stackable`,
  `defaultValue`. This is metadata about what an attribute means, not a
  value for any particular item.
- **`dgmTypeAttributes`**: the actual `(typeID, attributeID) → value`
  pairs — this is where "the Rifter has 350 armor HP" actually lives, split
  across `valueInt`/`valueFloat` depending on the attribute's data type.
  Source JSONL (`typeDogma.jsonl`) nests it per-type:
  ```json
  {"_key": 587, "dogmaAttributes": [{"attributeID": 9, "value": 350.0}, ...]}
  ```
- **`dgmEffects`** / **`dgmTypeEffects`**: same pattern for behaviors instead
  of numbers — an effect definition, and which types have which effects.
  `dgmEffects` also cross-references specific attributes by ID
  (`durationAttributeID`, `rangeAttributeID`, `falloffAttributeID`, ...) —
  that's how e.g. a weapon's effect knows which attribute holds its range.
- **`eveUnits`**: what an attribute's raw number actually means (m, m/s,
  %, seconds, ...) — join via `dgmAttributeTypes.unitID`.

To get "what are the Rifter's stats, human-readable":

```sql
SELECT at.displayName, ta.valueFloat, at.unitID
FROM dgmTypeAttributes ta
JOIN dgmAttributeTypes at ON at.attributeID = ta.attributeID
WHERE ta.typeID = 587;
```

## Industry / Blueprints

Manufacturing/research recipes, keyed by `typeID` = the blueprint's own
typeID (blueprints are themselves rows in `invTypes`).

- **`industryBlueprints`**: `typeID`, `maxProductionLimit` (max simultaneous
  production runs).
- **`industryActivity`**: one row per `(typeID, activityID)` — `time` the
  activity takes. Activity IDs: `1`=manufacturing, `3`=research time
  (time efficiency), `4`=research material (material efficiency),
  `5`=copying, `8`=invention, `11`=reaction.
- **`industryActivityMaterials`**: inputs — `(typeID, activityID,
  materialTypeID, quantity)`.
- **`industryActivityProducts`**: outputs — `(typeID, activityID,
  productTypeID, quantity)`.
- **`industryActivityProbabilities`**: success chance, invention only.
- **`industryActivitySkills`**: skill + level required per activity.

Source shape in `blueprints.jsonl` nests activities by name
(`manufacturing`, `copying`, `invention`, ...) which the loader maps to
numeric `activityID`s — see
`tableloader/tableFunctions/blueprints.py:activityIDs` for the exact
name→ID mapping if you're parsing the JSONL directly instead of querying SQL.

## Map / Universe

Spatial and organizational structure of New Eden.

```
mapRegions (regionID)
  └─ mapConstellations (constellationID, regionID)
       └─ mapSolarSystems (solarSystemID, constellationID, regionID, security)
            └─ mapDenormalize (itemID, solarSystemID, ...) — every celestial
```

**`mapDenormalize`** is the big one — every star, planet, moon, belt,
stargate, and station, each a row with `x`/`y`/`z` coordinates (in meters),
`typeID` (what kind of celestial), `groupID`, and which system/constellation/
region it's in. `itemID` here matches the IDs used elsewhere in-game (e.g.
in ESI location data) for the same celestial.

- `mapJumps` / `mapSolarSystemJumps` / `mapConstellationJumps` /
  `mapRegionJumps`: stargate connectivity at different granularities. The
  latter three are **derived tables** — built by the toolkit's own
  `buildjumps` step from `mapJumps`, not loaded straight from JSONL.
- `staStations` / `staServices`: NPC stations and what services they offer.
- Security status: `mapSolarSystems.security` is the raw float; note the
  well-known EVE convention of *displaying* it rounded to 1 decimal, which
  can shift a system across the highsec/lowsec (0.45 vs 0.5) boundary —
  don't just truncate when doing security-based logic.

## NPCs, agents, corporations

- **`crpNPCCorporations`**: the NPC corp directory (faction, description,
  standings baseline).
- **`npcCharacters`** / **`agtAgents`** / **`agtResearchAgents`**: individual
  NPC agents, which corp/station they're at, their agent type/level (from
  `agtAgentTypes`).
- **`chrFactions`** / **`chrRaces`** / **`chrBloodlines`** /
  **`chrAncestries`**: the character-creation taxonomy — also referenced by
  `invTypes.raceID` and `invTypes.factionID` for faction ships/items.

## Certificates, skins, ship tree, planetary — smaller domains

- **`certCerts`/`certSkills`/`certMasteries`**: the certificate system —
  which skills+levels a cert requires, and its mastery tiers.
- **`skins`/`skinMaterials`/`skinLicense`/`skinShip`**: ship reskins and
  which ships/materials each skin applies to. (`skinr*` tables are a
  separate, newer skin *customization* system — components/slots you mix
  yourself — don't confuse the two.)
- **`shipTree*`**: the in-game ship progression/recommendation tree shown to
  new players, not a gameplay-mechanical table.
- **`planetResources`/`planetSchematics*`**: Planetary Interaction — what
  resources a planet type yields, and PI factory schematics (inputs/outputs,
  analogous in shape to the blueprint activity tables above).

## Other domains

Smaller/newer domains, added in the loader's per-file split — see
[table-map.md](table-map.md) for exact tables and [columns.md](columns.md)
for their schemas rather than duplicating that detail here:

- **Industry facilities/filters**: `ramActivities`, `ramAssemblyLines`,
  `ramInstallationTypeContents`, `indModifierSources`, `indTargetFilters*` —
  station/structure-side manufacturing mechanics, distinct from the
  blueprint-side `industryActivity*` tables above.
- **Station operations**: `staOperations`, `staOperationServices`,
  `staOperationTypes`, `staStandingsRestrictions` — what a station *does*
  (repair, market, cloning, ...) and standing requirements to use it.
- **Corporation roles**: `crpRoleGroups`, `crpRoles`, `crpRoleRoleGroups` —
  the in-game corp member permission system.
- **Accounting**: `acctEntryTypes` — wallet journal entry type definitions.
- **Fighters**: `fighterAbilities`, `fighterAbilitiesByType`.
- **Schools/career**: `chrSchools`, `chrSchoolCareerAgents`,
  `chrSchoolStartingStations`, `chrSchoolMap`, `skillPlans*`,
  `expertSystems*` — new-player career agent/school content.
- **Type effects / system mechanics**: `aplProximityEffects`,
  `proximityTraps`, `linkWithShip*`, `sysDbuffEmitters*`, `sysWideEffects*`,
  `metenoxMoonDrills` — area-of-effect and system-wide buff/debuff mechanics.
- **Notifications**: `ntfTypes` — in-game notification type definitions.
- **`skinrSkinSlotToMaterial`**: extends the `skinr*` customization system
  above, mapping slots to allowed materials.

## What's *not* in the SDE

No player-specific data at all — no characters' skills/assets/wallets, no
live market orders, no corp/alliance membership, no killmails. That's all
[ESI](../eve-esi/SKILL.md) territory; the SDE is purely the static rules and
catalog the game runs on.
