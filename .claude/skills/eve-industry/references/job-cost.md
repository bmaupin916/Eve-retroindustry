# Job Cost

Source: https://wiki.eveuniversity.org/Manufacturing

## The formula

```
Total job cost = Estimated item value ×
    ((System cost index × Structure bonuses) + Facility tax + SCC surcharge + Alpha clone tax)
```

- **Estimated item value** = `Σ (material quantity × material adjusted price)`
  over every material the blueprint requires, calculated at ME0 (no material-
  efficiency bonus applied) — this is a valuation base, not what you'll
  actually spend on materials if the blueprint has ME research on it.
- **System cost index** — see below.
- **Structure bonuses** — engineering complexes/citadels can reduce the
  effective cost index contribution; varies by structure and rig fit.
- **Facility tax** — 0.25% fixed at NPC stations; player-structure owners set
  their own rate on player structures.
- **SCC surcharge** — fixed, currently 4% (raised from 0.25% as of
  2024-02-01 per CCP patch notes — this number moves, don't hardcode it into
  code without a source check).
- **Alpha clone tax** — 0.25%, applies only when the job is run by an Alpha
  (free-to-play) clone.
- All the percentage components together are **capped at 10% total**.

## System cost index

Conceptually: `Work hours done in system in past 28 days / Work hours done
in universe in past 28 days` — i.e. a rolling measure of how much industry
activity (by hours) has recently happened in that solar system relative to
the rest of New Eden, per activity type. Busier systems cost more to build in.

The wiki flags that this formula description is likely stale post-patch
("We have adjusted the System Cost Index formula to make it more volatile,"
version 21.05, 2023-09-12) — treat it as directional, not exact, and don't
try to reproduce the number by hand. Instead pull it live from ESI:

```
GET https://esi.evetech.net/latest/industry/systems/
```

No auth/scope required. Returns cost indices per solar system per activity:

```json
{
  "solar_system_id": 30020141,
  "cost_indices": [
    {"activity": "manufacturing", "cost_index": 0.0038},
    {"activity": "researching_time_efficiency", "cost_index": 0.0014},
    {"activity": "researching_material_efficiency", "cost_index": 0.0014},
    {"activity": "copying", "cost_index": 0.0014},
    {"activity": "invention", "cost_index": 0.0014},
    {"activity": "reaction", "cost_index": 0.0014}
  ]
}
```

(Verified live 2026-08-07: 5485 systems returned.) See [eve-esi](../../eve-esi/SKILL.md)
for general ESI conventions (User-Agent, X-Compatibility-Date, caching).

## Related ESI endpoints

- `GET /industry/facilities/` — no auth required; every player-owned
  structure with industry services online (facility_id, owner_id, type_id,
  system/region). Verified live: 2321 facilities.
- `GET /characters/{character_id}/blueprints` — requires
  `esi-characters.read_blueprints.v1`; a character's BPOs/BPCs including
  runs remaining and current ME/TE.
- `GET /characters/{character_id}/industry/jobs` — requires
  `esi-industry.read_character_jobs.v1`.
- `GET /corporations/{corporation_id}/blueprints` — requires
  `esi-corporations.read_blueprints.v1`.
- `GET /corporations/{corporation_id}/industry/jobs` — requires
  `esi-industry.read_corporation_jobs.v1`.

(Scopes verified against the live OpenAPI spec — see
[eve-esi's openapi-spec.md](../../eve-esi/references/openapi-spec.md) for how
to look these up yourself rather than trusting a hardcoded list, since CCP
does add/rename scopes.)

## Selecting Tech 1 items to build (profitability checklist)

From the wiki's guidance on picking what to manufacture:

- **Inexpensive materials**: keep material cost below roughly 1% of your net
  worth as a risk-sizing rule of thumb, not a hard game mechanic.
- **Margin**: aim for at least ~10% profit per item; compare both ISK and
  percentage margin, since a high-percentage low-ISK item may not be worth
  the slot. 80%+ margins exist but are rare and usually short-lived (patched
  or arbitraged away).
- **Volume**: check the Market window's Price History → daily volume. A
  profitable item nobody buys is a stockpile, not income.

There's no shortcut around this — it's ongoing market research, not a fixed
list of "good items."
