# Material Efficiency (ME) / Time Efficiency (TE), skills, and job mechanics

Source: https://wiki.eveuniversity.org/Manufacturing

## Skills

- **Industry**: -4% manufacturing time per level. Level 1 is the baseline
  requirement to build anything; level 3 is a prerequisite for the advanced
  manufacturing skills below.
- **Advanced Industry**: -3% time per level, across manufacturing, research,
  *and* invention — stacks with Industry, so gains compound with run count
  and especially matter on large ships/capitals where job time is long.
- **Mass Production**: +1 concurrent manufacturing job slot per level (base
  is 1 slot). Level V = 6 total slots.
- **Advanced Mass Production**: unlocks once Mass Production is V; +1 slot
  per level. Level IV = 10 total slots; level V takes roughly 28 days to
  train, so it's a late-game investment, not a starter skill.
- **Supply Chain Management**: lets you start a job remotely, +5 jumps of
  range per level, capped at 25 jumps at level V. Convenience, not a
  requirement — don't block on it.

If a blueprint's START button stays greyed out, the blockage is usually a
blueprint-specific skill requirement (not one of the above) — check the
blueprint's own skill list, not just your general industry skills.
[references/sde-tables.md](sde-tables.md) shows where to query that
(`industryActivitySkills`).

## Facility bonuses

- NPC stations: no bonus — the baseline.
- Engineering complexes / citadels with the manufacturing service module
  online: **1% material savings**, and **15–30% time savings** depending on
  structure size (larger structures give more).
  - Structures can add further bonuses on top via fitted rigs.

### Looking up exact bonus numbers (SDE-verified, don't guess)

The qualitative "1% material, 15-30% time" above is the general rule, but the
*exact* per-structure numbers are queryable rather than estimated, via two
tables added in the SDE loader's per-file split:

**Base structure bonus** — `indModifierSources` maps a structure's typeID to
which dogma attribute drives which activity/modifier-type combo:

```sql
SELECT typeID, activityName, modifierType, dogmaAttributeID
FROM indModifierSources WHERE typeID = 35825;  -- Raitaru
```

`dogmaAttributeID` is always one of: `2600` (`strEngMatBonus`, material),
`2601` (`strEngCostBonus`, job-installation-cost), `2602`
(`strEngTimeBonus`, time). Join to `dgmTypeAttributes` for the actual
multiplier (verified against `eve.db`):

| Structure | material (2600) | cost (2601) | time (2602) |
|---|---|---|---|
| Raitaru  | 0.99 (-1%) | 0.97 (-3%) | 0.85 (-15%) |
| Azbel    | 0.99 (-1%) | 0.96 (-4%) | 0.80 (-20%) |
| Sotiyo   | 0.99 (-1%) | 0.95 (-5%) | 0.70 (-30%) |

These are multiplicative factors (0.85 = "×0.85", i.e. -15%), not percentages
to subtract by hand.

**Rig bonuses** are a *separate* additive layer on top, using different
attributes on the rig's own typeID — `attributeEngRigMatBonus`,
`attributeEngRigCostBonus`, `attributeEngRigTimeBonus` (percentage points,
e.g. `-2.0` = -2%). Rig bonuses **scale up in lower security space**, via
`hiSecModifier`/`lowSecModifier`/`nullSecModifier` attributes on the same
typeID (e.g. a T1 small-ship ME rig: highsec ×1.0, lowsec ×1.9, null/WH
×2.1 applied to the base -2%). Always check which space you're building in
before assuming a rig's headline number applies as-is.

Rigs are scope-restricted by ship/item group — `indTargetFilters` +
`indTargetFilterCategories`/`indTargetFilterGroups` is the catalog of what a
given scope name (`Small T1 Ships`, `Capital Components`, `Hybrid
Reactions`, ...) actually covers, e.g. "Small T1 Ships" =
`invGroups.groupID` 25/31/420 (Frigate/Shuttle/Destroyer). Use it to confirm
a rig you're pricing actually applies to the item you're building, rather
than assuming from the rig's name alone.

Full column-level schema for all of these: see `eve-sde`'s
[columns.md](../../eve-sde/references/columns.md) (modules
`industrymodifiersources`, `industrytargetfilters`).

## Running jobs

1. Pick the blueprint in the Industry window.
2. Pick the job type (manufacturing, research, copying, invention, reaction).
3. Set the run count.
4. Point input/output locations at the right containers/hangars if not using
   the default station hangar.
5. Start.

Progress shows in the Jobs tab; on completion, a Deliver button returns the
output item(s) and the blueprint (BPO, or the now-consumed-run BPC) to the
output location. **Cancelling a job at any time forfeits material and
installation costs already spent — nothing is refunded.**

## Rounding — the gotcha

Material quantities are rounded **per job**, not per run, after ME reduction
is applied to the job's *total* material requirement. This means:

> "a single industry job with 3 runs can use *less* material than 3 single
> jobs from the same blueprint!"

— because each separate 1-run job re-applies rounding to a smaller number.
Batch runs into fewer, larger jobs when you want to capture ME rounding
gains.

The floor, however, doesn't go below 1 unit of each material **per run**:

> "the manufacturing job will require at least one item of each type per
> run. With 100 runs and 10% material reduction, you would assume that you
> would need 90 items but you still need 100 items."

i.e. if a material's base per-run requirement is already 1, no amount of ME
will reduce the *total* below `run count × 1` for that material — ME only
has room to bite on materials whose per-run quantity is large enough that a
percentage cut still rounds to ≥1 savings.
