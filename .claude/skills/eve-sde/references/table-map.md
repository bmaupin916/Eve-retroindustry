# JSONL File → Module → DB Table Map

> **Scope note.** This file documents the **full** SDE as loaded by the
> external `jsonl-evesde` toolkit — 176 tables, classic names (`invTypes`,
> `industryActivityMaterials`). **This project ships a 14-table subset under
> different names** (`sde_types`, `sde_blueprint_materials`), so most of what
> follows is not queryable against `sde_base.db` without importing it first.
> See the [parent skill](../SKILL.md) for the name mapping and what is present.
> Absolute paths below belong to the toolkit author's environment.

Pulled directly from `/home/scripts/jsonl-evesde/Load.py`'s `LOADERS` list — this is the authoritative map of which JSONL file(s) feed which loader module, and which SQL tables that module owns. As of the loader's per-file split (commit `32a450e` onward), this is 86 module entries, mostly one JSONL file per module. If this ever looks out of date, that file is the source of truth; re-read it. For each table's actual columns, see [columns.md](columns.md).

| Module | Source JSONL files | DB tables |
|---|---|---|
| `types` | types.jsonl | invTypes, invMetaTypes |
| `typebonus` | typebonus.jsonl | invTraits |
| `typematerials` | typematerials.jsonl | invTypeMaterials |
| `typedogma` | typedogma.jsonl | dgmTypeAttributes, dgmTypeEffects |
| `groups` | groups.jsonl | invGroups |
| `categories` | categories.jsonl | invCategories |
| `metagroups` | metagroups.jsonl | invMetaGroups |
| `marketgroups` | marketgroups.jsonl | invMarketGroups |
| `typelist` | typelists.jsonl | typeListsHeader, typeListsIncludedTypeIDs, typeListsExcludedTypeIDs, typeListsIncludedGroupIDs, typeListsExcludedGroupIDs, typeListsIncludedCategoryIDs, typeListsExcludedCategoryIDs |
| `dogmaattributecategories` | dogmaattributecategories.jsonl | dgmAttributeCategories |
| `dogmaattributes` | dogmaattributes.jsonl | dgmAttributeTypes |
| `dogmaeffects` | dogmaeffects.jsonl | dgmEffects |
| `dogmaunits` | dogmaunits.jsonl | eveUnits |
| `shipskills` *(derived, depends on types, typedogma, dogmaattributecategories, dogmaattributes, dogmaeffects, dogmaunits)* | — | shipSkills |
| `blueprints` | blueprints.jsonl | industryBlueprints, industryActivity, industryActivityMaterials, industryActivityProducts, industryActivityProbabilities, industryActivitySkills |
| `corporationactivities` | corporationactivities.jsonl | crpActivities |
| `npccorporationdivisions` | npccorporationdivisions.jsonl | crpNPCDivisions |
| `npccorporations` | npccorporations.jsonl | crpNPCCorporations, crpNPCCorporationDivisions, crpNPCCorporationTrades |
| `map` | mapregions.jsonl, mapconstellations.jsonl, mapsolarsystems.jsonl, mapstars.jsonl, mapplanets.jsonl, mapmoons.jsonl, mapasteroidbelts.jsonl, mapstargates.jsonl, mapsecondarysuns.jsonl, npcstations.jsonl, stationservices.jsonl, landmarks.jsonl | mapRegions, mapConstellations, mapSolarSystems, mapDenormalize, mapCelestialStatistics, mapCelestialGraphics, mapJumps, mapLocationWormholeClasses, mapLandmarks, staStations, staServices |
| `planetary` | planetresources.jsonl, planetschematics.jsonl | planetResources, planetSchematics, planetSchematicsPinMap, planetSchematicsTypeMap |
| `buildjumps` *(derived, depends on map)* | — | mapSolarSystemJumps, mapRegionJumps, mapConstellationJumps |
| `agents` | agenttypes.jsonl, agentsinspace.jsonl | agtAgentTypes, agtAgentsInSpace |
| `ancestries` | ancestries.jsonl | chrAncestries |
| `bloodlines` | bloodlines.jsonl | chrBloodlines |
| `characterattributes` | characterattributes.jsonl | chrAttributes |
| `factions` | factions.jsonl | chrFactions |
| `races` | races.jsonl | chrRaces |
| `npccharacters` | npccharacters.jsonl | npcCharacters, agtAgents, agtResearchAgents |
| `certificates` | certificates.jsonl | certCerts, certSkills |
| `masteries` | masteries.jsonl | certMasteries |
| `skinmaterials` | skinmaterials.jsonl | skinMaterials |
| `skins` | skins.jsonl | skins, skinShip |
| `skinlicenses` | skinlicenses.jsonl | skinLicense |
| `graphics` | graphicmaterialsets.jsonl, graphics.jsonl, icons.jsonl | graphicMaterialSets, eveGraphics, eveIcons |
| `invnames` *(derived, depends on map, types, npccorporations, npccharacters)* | — | invNames, invUniqueNames |
| `clonegrades` | clonegrades.jsonl | chrCloneGrades, chrCloneGradeSkills |
| `charactertitles` | charactertitles.jsonl | chrTitles |
| `archetypes` | archetypes.jsonl | dungeonArchetypes |
| `missions` | missions.jsonl | mstMissions, mstMissionMessages, mstMissionExtraStandings |
| `epicarcs` | epicarcs.jsonl | epicArcs, epicArcMissions, epicArcMissionNextMissions |
| `dungeons` | dungeons.jsonl | dungeons, dungeonAllowedShips |
| `shiptreeelements` | shiptreeelements.jsonl | shipTreeElements |
| `shiptreegroups` | shiptreegroups.jsonl | shipTreeGroups, shipTreeGroupElements, shipTreeGroupPreReqSkills |
| `shiptreefactions` | shiptreefactions.jsonl | shipTreeFactions, shipTreeFactionElements |
| `typeelements` | typeelements.jsonl | typeElements |
| `military` | militarycampaigns.jsonl, militarycampaignobjectives.jsonl | milCampaigns, milCampaignObjectives, milCampaignObjContentTags |
| `mercenary` | mercenarytacticaloperations.jsonl | mercenaryTacticalOperations |
| `sovereigntyupgrades` | sovereigntyupgrades.jsonl | sovereigntyUpgrades |
| `skinrcomponentcategories` | skinrcomponentcategories.jsonl | skinrComponentCategories |
| `skinrcomponentrarities` | skinrcomponentrarities.jsonl | skinrComponentRarities |
| `skinrcomponentpointvalues` | skinrcomponentpointvalues.jsonl | skinrComponentPointValues |
| `skinrcomponents` | skinrcomponents.jsonl | skinrComponents, skinrComponentTypes |
| `skinrslotcategories` | skinrslotcategories.jsonl | skinrSlotCategories |
| `skinrslotnames` | skinrslotnames.jsonl | skinrSlotNames |
| `skinrslots` | skinrslots.jsonl | skinrSlots, skinrSlotAllowedCategories |
| `skinrslotconfigurations` | skinrslotconfigurations.jsonl | skinrSlotConfigurations, skinrSlotConfigurationSlots, skinrSlotConfigurationShips |
| `skinrtierthresholds` | skinrtierthresholds.jsonl | skinrTierThresholds |
| `compressible` | compressibletypes.jsonl | compressibleTypes |
| `freelance` | freelancejobschemas.jsonl | freelanceJobSchemas, freelanceJobSchemaContentTags, freelanceJobSchemaParameters |
| `translationlanguages` | translationlanguages.jsonl | trnTranslationLanguages |
| `contraband` | contrabandtypes.jsonl | invContrabandTypes |
| `controltower` | controltowerresources.jsonl | invControlTowerResources |
| `industryactivities` | industryactivities.jsonl | ramActivities |
| `industryassemblylines` | industryassemblylines.jsonl | ramAssemblyLines, ramAssemblyLineTypeDetailPerGroup |
| `industryinstallationtypes` | industryinstallationtypes.jsonl | ramInstallationTypeContents |
| `industrymodifiersources` | industrymodifiersources.jsonl | indModifierSources |
| `industrytargetfilters` | industrytargetfilters.jsonl | indTargetFilters, indTargetFilterCategories, indTargetFilterGroups |
| `stationoperations` | stationoperations.jsonl | staOperations, staOperationServices, staOperationTypes |
| `stationstandingsrestrictions` | stationstandingsrestrictions.jsonl | staStandingsRestrictions |
| `corporationrolegroups` | corporationrolegroups.jsonl | crpRoleGroups |
| `corporationroles` | corporationroles.jsonl | crpRoles, crpRoleRoleGroups |
| `accountingentrytypes` | accountingentrytypes.jsonl | acctEntryTypes |
| `fighterabilities` | fighterabilities.jsonl | fighterAbilities |
| `fighterabilitiesbytype` | fighterabilitiesbytype.jsonl | fighterAbilitiesByType |
| `schools` | schools.jsonl | chrSchools, chrSchoolCareerAgents, chrSchoolStartingStations |
| `schoolmap` | schoolmap.jsonl | chrSchoolMap |
| `skillplans` | skillplans.jsonl | skillPlans, skillPlanMilestones, skillPlanSkillRequirements |
| `expertsystems` | expertsystems.jsonl | expertSystems, expertSystemSkillsGranted |
| `appliedproximityeffects` | appliedproximityeffects.jsonl | aplProximityEffects, aplProximityEffectDbuffs |
| `proximitytrap` | proximitytrap.jsonl | proximityTraps |
| `linkwithship` | linkwithship.jsonl | linkWithShip, linkWithShipDbuffs |
| `systemdbuffemitters` | systemdbuffemitters.jsonl | sysDbuffEmitters, sysDbuffEmitterDbuffs |
| `systemwideeffects` | systemwideeffects.jsonl | sysWideEffects, sysWideEffectDbuffs |
| `metenoxmoondrill` | metenoxmoondrill.jsonl | metenoxMoonDrills |
| `notificationtypes` | notificationtypes.jsonl | ntfTypes |
| `skinrslotstomaterials` | skinrslotstomaterials.jsonl | skinrSkinSlotToMaterial |## Shared tables

Several modules write into shared tables that don't belong to any single
module's `tables` list. Never assume these belong to one module:

### `trnTranslations`

| tcIDs | Owning module |
|---|---|
| 6 | `categories` |
| 7 | `groups` |
| 8, 33 | `types` |
| 11 | `bloodlines` |
| 12 | `ancestries` |
| 14 | `marketgroups` |
| 15 | `metagroups` |
| 16 | `races` |
| 17 | `certificates` |
| 19 | `factions` |
| 20 | `npccorporations` |
| 24 | `npccorporationdivisions` |
| 35, 36 | `accountingentrytypes` |
| 37 | `corporationrolegroups` |
| 38, 39 | `corporationroles` |
| 40, 41 | `fighterabilities` |
| 42, 43, 44, 45 | `schools` |
| 46, 47 | `skillplans` |
| 48 | `notificationtypes` |
| 49, 50 | `stationoperations` |

### `trnTranslationColumns`

New since the per-file loader split: every module that owns `trnTranslations`
rows also writes matching rows here (one row per translated column, mapping
`tcID` → the table/column/PK it localizes). Same owner list as
`trnTranslations` above. It's a metadata table describing *which* columns are
translated, not the translated values themselves — `trnTranslations` holds
those.

## Not yet in the map above

Verified by diffing `/opt/sde/files/*.jsonl` against every filename `Load.py`
references — these exist on disk but aren't wired into any loader, so their
data is only reachable by reading the JSONL directly, not via SQL:

- `dbuffCollections.jsonl`
- `dynamicItemAttributes.jsonl`
- `_sde.jsonl` (SDE build metadata, not game data — see
  [toolkit.md](toolkit.md) for the build-number field it contains)

(`stationOperations.jsonl` was wired in during the per-file loader split —
no longer on this list.)

Re-run this check yourself if the toolkit may have changed since:

```bash
comm -23 \
  <(ls /opt/sde/files/*.jsonl | xargs -n1 basename | tr 'A-Z' 'a-z' | sort) \
  <(grep -oP "(?<=')[a-zA-Z]+\.jsonl(?=')" /home/scripts/jsonl-evesde/Load.py | tr 'A-Z' 'a-z' | sort -u)
```

## Quick lookups

```bash
# Which JSONL file(s) feed a table? e.g. "where does industryActivity come from"
grep -B5 "'industryActivity'" /home/scripts/jsonl-evesde/Load.py | grep -A10 "module_name"

# Full list of every table in the schema
cd /home/scripts/jsonl-evesde && .venv/bin/python -c "
from tableloader.tables import metadataCreator
print(len(metadataCreator(None).tables))"

# A table's exact columns/types — see columns.md's regeneration command, or:
grep -n "Table('<tableName>'" /home/scripts/jsonl-evesde/tableloader/tables.py
```
