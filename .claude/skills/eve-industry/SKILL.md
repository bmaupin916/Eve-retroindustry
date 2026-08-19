---
name: eve-industry
description: Use when working with EVE Online's industry system — manufacturing, invention, copying, research (ME/TE), reactions, capital/supercapital construction, or job cost calculation. Covers blueprints (BPO/BPC), the Tech 1/2/3 and capital production chains, required skills, and how job cost/system cost index are calculated. Triggers on "manufacturing", "blueprint", "BPO", "BPC", "ME", "TE", "invention", "industry job", "system cost index", or questions about building ships/modules/reactions in EVE.
---

# EVE Online Industry

Manufacturing turns a blueprint plus input materials into an output item via a
timed industry job. This skill covers the production chain conceptually and
the cost/job mechanics; for the underlying data (blueprint materials,
products, skill requirements) see [eve-sde](../eve-sde/SKILL.md), and for
querying/managing live jobs via the API see [eve-esi](../eve-esi/SKILL.md).

Source of truth for the conceptual/mechanical content in this skill:
https://wiki.eveuniversity.org/Manufacturing (re-fetch it if something here
looks stale — CCP tunes tax/index numbers periodically).

## Decision guide

- **"What materials/skills/time does blueprint X need?"** → query
  `industryActivity`, `industryActivityMaterials`, `industryActivityProducts`,
  `industryActivitySkills` in `eve.db` — see
  [references/sde-tables.md](references/sde-tables.md) for the schema and
  the `activityID` mapping (manufacturing=1, invention=8, copying=5, ...).
- **"How much will this job cost?"** →
  [references/job-cost.md](references/job-cost.md) for the full formula
  (estimated item value × cost index/tax factors), including how to pull the
  live system cost index from ESI.
- **"What's ME/TE, and how do runs/rounding work?"** →
  [references/me-te.md](references/me-te.md).
- **"How do I get/produce a Tech 2 blueprint?"** →
  [references/invention.md](references/invention.md) — invention, copying,
  and research jobs, plus the science skill pairs.
- **"What's different about Tech 3, capitals, boosters, or reactions?"** →
  [references/production-chains.md](references/production-chains.md).
- **"How do I query or manage jobs via the API?"** →
  [eve-esi](../eve-esi/SKILL.md) — `/industry/jobs`, `/industry/systems`,
  `/industry/facilities`, `/characters/{id}/blueprints` (scopes noted in
  [references/job-cost.md](references/job-cost.md)).

## Key facts to not get wrong

- **BPOs** (Blueprint Originals) have infinite runs; **BPCs** (Blueprint
  Copies) have a fixed run count baked in when copied/invented. Manufacturing
  consumes one run per job (or up to the run count if running multiple in one
  job) and does not consume the BPO itself — only a BPC's remaining runs.
- Most items produce **1 unit per run**; ammunition and a few other items
  produce in stacks (e.g. 100 per run). Check `industryActivityProducts.quantity`
  — don't assume 1:1.
- **Rounding happens per job, not per run.** A single job with N runs can use
  *less* total material than N separate 1-run jobs, because ME rounding is
  applied once to the job's total material need rather than N times. But a
  job always needs **at least 1 of each material type per run**, even after
  ME rounding would otherwise round a small quantity to 0 — e.g. 100 runs at
  10% material reduction still needs a full 100 units of a material whose
  base per-run quantity was already 1, not 90.
- Tech 2 (and most Tech 3) BPCs cannot be bought as BPOs anymore — Tech 2 BPOs
  were a one-time "blueprint lottery" seeding and no new ones enter the game.
  The only way to get a Tech 2 BPC today is **invention**.
  [references/invention.md](references/invention.md)
- Moon-material reactions and Tech 2/3 component chains require lowsec/
  nullsec/wormhole refineries — they cannot be run from highsec.
  [references/production-chains.md](references/production-chains.md)
- Cancelling a job returns nothing — no material or installation-cost refund.
- Structure/rig ME/TE/cost bonuses are exact, queryable numbers via
  `indModifierSources` + `dgmTypeAttributes` — don't estimate them. And don't
  confuse that table with `ramActivities`/`ramInstallationTypeContents`,
  which model the legacy NPC-station/POS system, not modern structures.
  [references/me-te.md](references/me-te.md),
  [references/sde-tables.md](references/sde-tables.md)
