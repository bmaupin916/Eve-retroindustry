# The jsonl-evesde Toolkit

> **Not part of this project, and not present on this machine.** This repo
> imports the SDE with its own `import_sde.py`, which produces the 14-table
> subset described in the parent skill. This file is kept for the one case it
> answers: needing a table that subset does not have (dogma attributes, the
> universe map, `indModifierSources`), where a full load into a scratch
> database is the shortest route. The paths below are the toolkit author's own
> environment — substitute wherever you actually cloned it.

`jsonl-evesde` loads the JSONL SDE into SQLite/MySQL/PostgreSQL/MSSQL. This is a
condensed guide for using it from *outside* that repo. If you're actively editing the toolkit's own code, read
`/home/scripts/jsonl-evesde/CLAUDE.md` directly instead — it has the full
architecture notes (loader dispatch, transaction handling, how to add a
table) and is the authoritative source; don't duplicate guesses from memory
when that file is one read away.

## Fastest path: just query the existing SQLite file

Before loading anything, check whether `eve.db` already has what you need:

```bash
sqlite3 /home/scripts/jsonl-evesde/eve.db ".tables"
sqlite3 /home/scripts/jsonl-evesde/eve.db "SELECT * FROM invTypes WHERE typeID = 587;"
```

## Running a full load yourself

```bash
cd /home/scripts/jsonl-evesde
.venv/bin/python Load.py sqlite          # always use .venv's python, not system python3
.venv/bin/python Load.py sqlite de       # optional 2nd arg: language for localized strings, default 'en'
```

Valid targets (defined in `sdeloader.cfg`, copy from `sdeloader.cfg-example`
if it doesn't exist): `sqlite`, `mysql`, `postgres`, `postgresschema`,
`mssql`. A full load **drops and recreates every table** — don't run it
against a database with other data you care about.

## Update mode (only reload what changed)

Set `SDE_CHANGED_FILES` to a newline-separated list of changed JSONL
basenames; only loader modules whose files (or upstream dependencies, for
derived tables) appear in that list get dropped/reloaded:

```bash
SDE_CHANGED_FILES=$'types.jsonl\ngroups.jsonl' .venv/bin/python Load.py sqlite
```

`run-conversion.sh` sets this automatically by diffing SDE releases in its
own git repo under `/opt/sde/files/` — you rarely need to set it by hand
unless testing a specific loader module in isolation.

## Full automated pipeline

```bash
./run-conversion.sh              # checks for a new SDE build, skips if unchanged
./run-conversion.sh --force      # runs the full pipeline regardless
./run-conversion.sh --reprocess  # skips download; reloads from the SDE already on disk
```

`--reprocess` is for after fixing/adding a loader — it re-runs the load
against files already present, diffing `git diff HEAD~1` in the SDE's local
git history to figure out which JSONL files changed (or a full load if
there's no previous commit).

Downloads the latest CCP SDE release, commits it to a local git history (so
update-mode diffing works), runs `Load.py` per configured DB target, exports
compressed dumps, and updates `latest-*` symlinks. Requires
`run-conversion.cfg` (copy from `run-conversion.cfg-example`) and uses
`flock` to prevent concurrent runs. Typically cron'd, e.g. every 4 hours.

The current build number/release date on disk is in `_sde.jsonl` (not loaded
into any table — it's pipeline metadata, not game data):

```bash
cat /opt/sde/files/_sde.jsonl
# {"_key": "sde", "buildNumber": 3457062, "releaseDate": "2026-08-05T11:06:46Z"}
```

## Adding support for a new table

This is a toolkit code change, not a data question — go read
`/home/scripts/jsonl-evesde/CLAUDE.md`'s "Adding a new table" section rather
than reconstructing the steps here; it covers the four files that need
touching (`tables.py`, a `tableFunctions/` module, `__init__.py`,
`Load.py`'s `LOADERS`) plus the JSONL streaming/nullable-dict/transaction
conventions used throughout.
