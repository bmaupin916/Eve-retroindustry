---
name: eft-format
description: Use when reading, writing, or converting EVE Online ship fits in EFT ("EVE Fitting Tool") text format — the plain-text fit format used by the in-game "Copy/Import from Clipboard" fitting actions and every third-party fitting tool. Triggers on "EFT format", "EFT fit", "copy fit to clipboard", "import fit", or when asked to produce/parse a ship fitting as text.
---

# EFT Fitting Format

EFT stands for "EVE Fitting Tool" — a third-party app that's long gone, but the
plain-text format it popularized is now the de facto standard: it's what the
in-game fitting window produces via "Copy to Clipboard" and accepts via "Import
from Clipboard", and what virtually every third-party fitting site/tool
(pyfa, EVE Workbench, zKillboard, Google Sheets fit lists, forum posts) reads
and writes.

Source of truth: CCP's ESI docs, `docs/guides/fitting.md` in the esi-docs
repository (https://github.com/esi/esi-docs) — it also covers the DNA and XML
fit formats if a task needs those. This project does not ship fitting features,
so nothing here is wired to the codebase; it is reference only.

## Structure

The fit is plain text, line-based, organized into sections in this fixed order:

1. **Header** — `[Hull Name, Fit Name]`, both fields inside one pair of square
   brackets, comma-separated.
2. **Low slot modules** — one per line.
3. **Mid slot modules** — one per line, with charge (if loaded) as
   `Module Name, Charge Name` on the same line.
4. **High slot modules** — same charge convention, e.g.
   `125mm Railgun I, Antimatter Charge S`.
5. **Rigs**
6. **Subsystems** (Tech 3 ships only)
7. **Services** (structure fits only)
8. **Drones / fighters** in the bay, with count suffix: `Warrior II x2`.
9. **Cargo bay items**, with count suffix: `Antimatter Charge M x42`.

**Blank lines are the section separators**, not headers or labels:
- One blank line between sections 2–7 (low → mid → high → rigs → subsystems →
  services), *only where the fit actually has modules in the next section* —
  don't assume every fit has all seven.
- **Two** blank lines between section 7 (services/rigs/whatever the last
  fitted section is) and section 8 (drones), and again before section 9
  (cargo).

## Rules and edge cases

- **Empty slots**: written as `[Empty Low slot]`, `[Empty Med slot]`,
  `[Empty High slot]`, `[Empty Rig slot]`, `[Empty Service slot]`. The game's
  own "Copy to Clipboard" never emits these (it just omits the line), but
  parsers must accept them — expect to see them in hand-written or
  tool-generated fits.
- **Offline modules**: suffixed with `/offline`, e.g.
  `Inertial Stabilizers II /offline`. Cosmetic only — the game's importer
  ignores the suffix and fits the module online regardless. Don't rely on it
  to actually produce an offlined module.
- **Counts**: `Item Name xN` for both drones/fighters and cargo items — no
  count suffix means a quantity of 1 (drones bay) or is simply omitted
  (rarely used for cargo, but a bare line without `xN` is still valid).
- **Localization**: item and hull names can be in any localized language, not
  just English. When generating a fit for a specific audience, match their
  client's language; when parsing, don't assume English names.
- **Charges vs. no charges**: only mid and high slot modules take the
  `Module, Charge` comma form. Low slots and rigs never carry a charge.

## Example

```
[Heron Navy Issue, Deepflow Rift Dredger]
Inertial Stabilizers II
Inertial Stabilizers II /offline

Scan Pinpointing Array II
Scan Rangefinding Array II
Scan Acquisition Array II
Compact EM Shield Amplifier
Compact Thermal Shield Amplifier

Small Tractor Beam II
Small Tractor Beam II
Core Probe Launcher II
Improved Cloaking Device II

Small Gravity Capacitor Upgrade II
Small Core Defense Field Extender I




Sisters Core Scanner Probe x8
```

Note the two blank lines before the cargo line (`Sisters Core Scanner Probe
x8`) — there's no drone bay content here, so that section is skipped
entirely but its separator still counts.

## Writing a fit in this format

When asked to produce a fit as EFT text (e.g. so the user can paste it
straight into the in-game importer):
- Use exact, correctly-capitalized in-game item names — a typo or wrong meta
  variant (`II` vs `I` vs a named/faction variant) will fail to import or
  silently substitute the wrong module.
- Preserve section order and blank-line separator rules exactly; the in-game
  importer is strict about this.
- Don't add commentary inside the block — anything other than the format
  above will break the paste-in import.
