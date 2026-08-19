---
name: eve-sde
description: Use when working with EVE Online's Static Data Export (SDE) — the game's static database of types/items, dogma attributes, industry/blueprints, the universe map, NPCs, and related reference data. Covers what the SDE is, its JSONL source format (which this project's import_sde.py consumes), which table holds what, this repo's 14-table sde_base.db subset and how its names map to the classic ones, and the external jsonl-evesde toolkit for loading the full SDE when a table the subset lacks is needed. Triggers on "SDE", "static data export", "invTypes", "dogma", "EVE Online database", "typeID", or questions about EVE item/ship/blueprint/map data.
---

# EVE Online SDE (Static Data Export)

The SDE is CCP's dump of EVE Online's static game data: every item/ship/skill
(`invTypes`), their attributes and effects (`dogma`), manufacturing recipes
(`industryActivity*`/`industryBlueprints`), the universe map
(`mapDenormalize`, `mapSolarSystems`, ...), NPC corporations/agents,
certificates, skins, and more. It does *not* contain player data — no
characters' assets, wallets, or market orders (that's what
[ESI](../eve-esi/SKILL.md) is for). It's released periodically by CCP as a
zip of YAML files; this environment works from a JSONL re-export of that same
data (see [references/jsonl-format.md](references/jsonl-format.md) for why
that format exists and how it's structured).

Background reading: https://www.fuzzwork.co.uk/2021/07/17/understanding-the-eve-online-sde-1/

## What this project actually has

**Paths here are repo-relative on purpose.** This project runs on Windows today
and will run on Linux later; nothing below should need editing when it moves.

- **`sde_base.db`** — a committed SQLite file at the repo root, ~10 MB. It is a
  **subset**, not the full SDE: 14 tables and 161,765 rows, holding only what
  this app models (types, groups, market groups, blueprints and their
  materials/products/skills, type materials, planet schematics, decryptors,
  datacore skills, skill time bonuses, and a `sde_build` row recording which
  CCP build it came from).
- **`eve_cache.db`** — the live database. The SDE tables are copied into it
  alongside the runtime tables, so a query can join `sde_types` to
  `market_price_cache` in one statement. That join is why the SDE cannot simply
  live in a database of its own.
- **`import_sde.py`** — builds either file from CCP's JSONL export, pinned to a
  build number, fetched by `app/sde/feed.py`. The format it parses is the same
  one [jsonl-format.md](references/jsonl-format.md) describes, so that file is
  about this project's own input, not a foreign toolchain.

```bash
python import_sde.py                      # newest build -> eve_cache.db
python import_sde.py --out sde_base.db    # rebuild the committed subset
python import_sde.py --build 3470007      # pin to a specific build
```

### Table names here are not the classic SDE names

Most SDE documentation — including the rest of this skill — uses the historical
names. This project uses its own, and the classic ones **do not exist** in
either database. Translate before querying:

| Classic SDE | This project |
|---|---|
| `invTypes` | `sde_types` |
| `invGroups` | `sde_groups` |
| `invMarketGroups` | `sde_market_groups` |
| `invTypeMaterials` | `sde_type_materials` |
| `industryBlueprints` | `sde_blueprints` |
| `industryActivityMaterials` | `sde_blueprint_materials` |
| `industryActivityProducts` | `sde_blueprint_products` |
| `industryActivitySkills` | `sde_blueprint_skills` |
| `planetSchematics` | `sde_planet_schematics` |

Columns are flattened too — `sde_types.type_id` and `.name`, not
`typeID`/`typeName`. Read the declaration in `app/db/schema.py` rather than
guessing; it is the single source of truth for every table here, and no DDL is
allowed to exist outside it.

**Not present**: `dgmTypeAttributes`, `mapSolarSystems`, `mapDenormalize`,
`indModifierSources`, and the other ~160 tables of the full SDE. Anything in
[domains.md](references/domains.md) or [columns.md](references/columns.md) that
is not in the table above would have to be imported first — see
[toolkit.md](references/toolkit.md), which describes a separate tool this repo
does not ship.

### Querying it

`sqlite3` is not assumed to be on PATH — it is not, on the current Windows box.
Use Python, which is:

```bash
python -c "import sqlite3; print(sqlite3.connect('sde_base.db').execute('SELECT name FROM sde_types WHERE type_id=587').fetchone())"
```

In application code go through `app.db.conn` rather than opening the file. Note
that `connect_to_path()` deliberately does **not** set `journal_mode`: that
setting rewrites the file it is applied to, and `sde_base.db` is committed.

## Decision guide

- **"What table has X data?"** →
  [references/table-map.md](references/table-map.md) — the full JSONL
  file → DB table mapping (102 files, 176 tables, 86 loader modules), pulled
  directly from the loader's own module list. Faster and more reliable than
  searching the JSONL files by hand.
- **"What columns does table X have?"** →
  [references/columns.md](references/columns.md) — every table's exact
  columns, types, and keys, generated by introspecting the toolkit's own
  SQLAlchemy schema (not hand-transcribed). Check here before writing a
  query instead of guessing a column name.
- **"How does the dogma/industry/map/NPC system work conceptually?"** →
  [references/domains.md](references/domains.md) — explains the major data
  domains, their key tables, and how they relate (e.g. how `dgmTypeAttributes`
  + `dgmAttributeTypes` combine to give an item's stats).
- **"What does a raw JSONL record look like, and how do I parse one?"** →
  [references/jsonl-format.md](references/jsonl-format.md) — the `_key` /
  localized-string-dict conventions shared across every file.
- **"Load a new SDE release for this project"** → `python import_sde.py`,
  which rebuilds `eve_cache.db` (or `sde_base.db` with `--out`) from CCP's
  JSONL export at a pinned build. That is the supported path here.
- **"I need a table the 14-table subset does not have"** → either extend
  `import_sde.py` and `app/db/schema.py` together (no DDL may live outside the
  declaration), or do a throwaway full load with the external toolkit —
  [references/toolkit.md](references/toolkit.md).
- **"I just need one value"** → query `sde_base.db` with the Python snippet
  above before writing any code.

## Key facts to not get wrong

- Every JSONL record's primary key is `_key`, not `id` or `typeID` — e.g. a
  line in `types.jsonl` has `"_key": 587` for typeID 587, not a `typeID`
  field.
- Localized strings (names, descriptions) are dicts keyed by language code:
  `"name": {"en": "...", "de": "...", "fr": "...", ...}`, not plain strings.
  Missing/removed content can show up as an explicit JSON `null` rather than
  an absent key — code that does `d.get('foo', {})` will get `None`, not
  `{}`, and crash on the next `.get()`. Guard with `(d.get('foo') or {})`.
  See [references/jsonl-format.md](references/jsonl-format.md).
- The relational schema has **no foreign keys** — every cross-table
  reference (`typeID`, `groupID`, `solarSystemID`, ...) is a plain unindexed-
  or indexed-but-unconstrained `INTEGER`. Don't assume the DB will catch a
  bad reference; the JSONL/SDE data itself is the source of truth for
  validity.
- `published: false` on an `invTypes` row means the item is inactive/removed
  from the game (an old blueprint, a design draft, a reskinned duplicate,
  etc.) — most tools should filter on `published = true` unless specifically
  looking at historical/unpublished data.
