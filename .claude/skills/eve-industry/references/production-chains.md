# Production chains: Tech 2 materials, Tech 3, capitals, boosters

Source: https://wiki.eveuniversity.org/Manufacturing

## Tech 2 materials

Tech 2 items are built from a wider material set than Tech 1 — typically:
a Tech 1 version of the analogous item, one or more Robotic Assembly
Modules (R.A.M.s, e.g. Armor/Hull Tech R.A.M.), extra minerals (notably
Morphite), and planetary materials (e.g. Construction Blocks). Example from
the wiki: a Tech 2 nanofiber needs a Tech 1 nanofiber + R.A.M.s + Morphite +
Construction Blocks.

**R.A.M.s**: nine variants exist for different construction domains
(Starship Tech, Ammunition Tech, Cybernetics, ...), each manufactured from
plain Tech 1-style mineral inputs.

**Moon materials**: sourced by moon mining at 0.5 security or lower, via a
refinery with a Moon Drill module fitted. This is corp/alliance-scale
infrastructure — the wiki is explicit that solo moon mining generally isn't
profitable. Basic moon materials (Chromium, Technetium, Tungsten, ...) react
at refineries into advanced materials (Tungsten Carbide, Fullerides,
Fermionic Condensates, ...). A viable business exists purely on the reaction
step — buy basic materials on the market, react them in a lowsec/nullsec
refinery, sell the advanced output — without ever mining.

**Advanced components**: built exclusively from moon materials, in racial
flavors (Amarr/Caldari/Gallente/Minmatar, colour-coded icons). Otherwise the
construction pattern mirrors Tech 1 manufacturing, just fed by moon
materials and gated by the relevant science skills.

**Planetary materials**: sourced via Planetary Industry (planet
interaction). The same character can run both PI and the manufacturing job
that consumes it, which cuts down on market purchases.

## Tech 3 (Strategic Cruisers / tactical destroyers)

A distinct construction process combining invention (using ancient relics
from relic sites) with datacores from data sites. Hulls and subsystems are
each built from their own BPCs, using materials gathered from w-space —
gas clouds (reacted at reactor arrays), Sleeper salvage, plus ordinary
minerals. Treat this as its own specialized pipeline, not a variant of
standard Tech 2 invention.

## Capital ship construction

A large up-front investment but described as "extremely lucrative." Gated
by **Capital Ship Construction** (14x):

| Level | Unlocks |
|---|---|
| 1 | Capital ship components, capital modules, freighters, Orca |
| 3 | Carriers, dreadnoughts, fighters, fighter-bombers |
| 4 | Supercarriers, jump freighters, Rorqual |
| 5 | Titans |

**Where you can build**:
- Freighters and the Orca: any manufacturing facility (no security
  restriction).
- Carriers, dreadnoughts, Rorqual: lowsec or nullsec stations only — cannot
  be built in highsec.
- Large/Extra-Large citadels or engineering complexes with a Standup Capital
  Shipyard I module online, lowsec/nullsec only.

**Supercapitals** (supercarriers, titans): require a Sotiyo engineering
complex with a Standup Supercapital Shipyard I module, and that module can
only be anchored in sovereign nullsec systems that have the Supercapital
Construction Facilities infrastructure upgrade active.

**Risk**: a ship under construction cannot dock anywhere except a Keepstar
once launched from the shipyard structure — there is no "put it away
unfinished" option for supercapitals. The wiki notes many titans have been
lost when a hostile fleet destroyed the construction structure mid-build
(historically POS-based; the same exposure risk applies to modern
engineering complexes).

## Booster production

Boosters are made from mytoserocin/cytoserocin gas, harvested from cosmic
signatures that only spawn in specific regions of known space. Requires the
**Drug Manufacturing** skill.

1. **Gas processing** — react raw gas into pure booster material at a
   refinery. Simple reactions need a Standup Biochemical Reactor I module,
   and only work in 0.4 security or lower. The secondary reagent depends on
   the target grade: Garbage (Synth), Water (Standard), Spirits or Oxygen
   (Improved, product-dependent), Hydrochloric Acid (Strong).
2. **Booster assembly** — a normal manufacturing job (no security
   restriction, highsec-capable) consuming the pure booster material,
   megacyte, and the booster's blueprint.

## Not covered here

Team-based ME/TE boosts (a since-removed mechanic the wiki keeps only as
historical reference) are gone from the live game — don't design around
them.
