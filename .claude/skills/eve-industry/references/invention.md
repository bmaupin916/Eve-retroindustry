# Tech 2 production: invention, copying, research

Source: https://wiki.eveuniversity.org/Manufacturing

## Where Tech 2 blueprints come from

Tech 2 BPOs essentially don't circulate: a fixed, tiny number were seeded
once in the historical "Blueprint lottery" and CCP has stated no new Tech 2
BPOs will ever enter the game. For practical purposes, **the only route to a
Tech 2 blueprint today is inventing a BPC** from a Tech 1 BPC (copied, so the
original T1 BPO survives) plus a datacore pair and, usually, a decryptor.
Invention is chance-based — it produces a limited-run BPC on success, nothing
on failure (materials are still consumed).

## Skills

### Ship construction (building the Tech 2 hull itself)

- **Mechanical Engineering** (5x) — required for all Tech 2 ships regardless
  of race/size.
- Race-specific: **Amarr/Caldari/Gallente/Minmatar Starship Engineering**
  (5x) — matches the hull's race.
- Size-tier: **Advanced Small/Medium/Large/Industrial Ship Construction**
  (2x / 5x / 8x / 3x respectively) — matches the hull's size class.

### Module/item construction and invention

Two skills from this list per item (the specific pair is item-dependent —
check the item's invention/manufacturing requirements, don't guess):

- Electromagnetic Physics (5x)
- Electronic Engineering (5x)
- Graviton Physics (5x)
- High Energy Physics (5x)
- Hydromagnetic Physics (5x)
- Laser Physics (5x)
- Mechanical Engineering (5x)
- Molecular Engineering (5x)
- Nanite Engineering (5x)
- Nuclear Physics (5x)
- Plasma Physics (5x)
- Quantum Physics (5x)
- Rocket Science (5x)

Most of these give +1% time efficiency per level on top of unlocking the
invention job itself.

## Storyline items (a narrower, separate case)

Storyline module BPCs come only from COSMOS missions and require rare
components sourced from data sites, relic sites, and COSMOS locations — not
from standard invention. Needs one of a set of Encryption Methods / alien
Technology skills at 5x depending on faction/origin (Amarr/Caldari/Gallente/
Minmatar Encryption Methods, Sleeper/Takmahl/Talocan/Yan Jung Technology).

## Querying invention/copy/research data

`industryActivity`-family tables in the SDE cover all five job types by
`activityID` — see [sde-tables.md](sde-tables.md) for the exact mapping and
schema (invention = 8, copying = 5, research_material = 4, research_time = 3,
manufacturing = 1). `industryActivityProbabilities` holds invention success
chance per output product.
