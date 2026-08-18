"""
Import the EVE Online static data export into SQLite.

Reads CCP's **JSONL** export, downloaded straight from their static-data service
and pinned to a build number — see `app/sde/feed.py` for why. The previous
version parsed YAML out of a hand-populated `data/` directory using PyYAML,
which is not in `requirements.txt`; that is why the dev-setup doc told you not
to run this script. It is now safe to run.

Usage:
    python import_sde.py                      # newest build -> eve_cache.db
    python import_sde.py --out sde_base.db    # fresh bundle DB from scratch
    python import_sde.py --build 3470007      # pin to a specific build
    python import_sde.py --zip some.zip       # use an archive already on disk
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
import zipfile

from rich.console import Console

from app.sde import feed

# Matches "1% reduction in manufacturing time" or "...in reaction time".
# Reactions skill (45746) has "...reaction time per skill level" — without
# this alternation it would be silently dropped from sde_skill_time_bonus.
_BONUS_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*%\s*reduction\s+in\s+(?:manufacturing|reaction)\s+time',
    re.IGNORECASE,
)

console = Console()

_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_ROOT, "eve_cache.db")
CACHE_DIR = os.path.join(_ROOT, "data", "sde-archives")

_SKILL_EXCLUDE = {3380, 3388}   # Handled separately in calc_job_time
_IMPLANT_GROUP = 743            # Zainou/manufacturing implants — not fetchable via ESI skills

# Activities imported from each blueprint. Invention and the two research
# activities are present in the source and deliberately not read yet — they land
# with the invention cost model. Adding a name here is all the importer needs.
_ACTIVITIES = ("manufacturing", "reaction")


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sde_types (
            type_id         INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            group_id        INTEGER,
            published       INTEGER DEFAULT 1,
            market_group_id INTEGER,
            -- Volume of ONE unit as it ships, in m3. PACKAGED, not assembled:
            -- an assembled Nidhoggur is 11,250,000 m3 and a packaged one is
            -- 1,300,000, and it is the packaged figure that decides what a
            -- hauler carries and therefore profit-per-m3. 829 types differ,
            -- all of them ships and containers. The column this replaced was
            -- named `volume` and held the assembled figure, which was wrong
            -- for exactly the items where it mattered most.
            packaged_volume REAL
        );

        CREATE TABLE IF NOT EXISTS sde_groups (
            group_id INTEGER PRIMARY KEY,
            name     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_materials (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,   -- manufacturing / reaction
            material_type_id   INTEGER NOT NULL,
            quantity           INTEGER NOT NULL,
            PRIMARY KEY (blueprint_type_id, activity, material_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_products (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,
            product_type_id    INTEGER NOT NULL,
            quantity           INTEGER NOT NULL,
            probability        REAL DEFAULT 1.0,
            PRIMARY KEY (blueprint_type_id, activity, product_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_blueprints (
            blueprint_type_id  INTEGER PRIMARY KEY,
            max_production_limit INTEGER DEFAULT 1,
            manufacturing_time   INTEGER DEFAULT 0,
            reaction_time        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_skills (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,
            skill_type_id      INTEGER NOT NULL,
            required_level     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (blueprint_type_id, activity, skill_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_skill_time_bonus (
            skill_type_id   INTEGER PRIMARY KEY,
            skill_name      TEXT NOT NULL,
            time_bonus_pct  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sde_planet_schematics (
            schematic_id    INTEGER PRIMARY KEY,
            name            TEXT,
            cycle_time      INTEGER,
            output_type_id  INTEGER,
            output_qty      INTEGER
        );

        CREATE TABLE IF NOT EXISTS sde_planet_schematic_materials (
            schematic_id    INTEGER NOT NULL,
            type_id         INTEGER NOT NULL,
            quantity        INTEGER NOT NULL,
            PRIMARY KEY (schematic_id, type_id)
        );

        -- Which SDE build this database was built from. Without it the only
        -- answer to "is this current?" is a row count, which cannot see a
        -- rebalance that changes values without changing how many there are.
        CREATE TABLE IF NOT EXISTS sde_build (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            build_number INTEGER NOT NULL,
            release_date TEXT,
            imported_at  REAL
        );

        CREATE INDEX IF NOT EXISTS idx_bp_product ON sde_blueprint_products(product_type_id);
        CREATE INDEX IF NOT EXISTS idx_bp_materials ON sde_blueprint_materials(blueprint_type_id, activity);
        CREATE INDEX IF NOT EXISTS idx_bp_skills ON sde_blueprint_skills(blueprint_type_id, activity);
    """)
    conn.commit()


def record_build(conn: sqlite3.Connection, build: feed.Build):
    conn.execute(
        "INSERT INTO sde_build (id, build_number, release_date, imported_at) "
        "VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "build_number=excluded.build_number, release_date=excluded.release_date, "
        "imported_at=excluded.imported_at",
        (build.number, build.release_date, time.time()),
    )
    conn.commit()


def import_types(conn: sqlite3.Connection, z: zipfile.ZipFile) -> int:
    """types -> sde_types, and skill time bonuses in the same pass.

    One pass, because the descriptions the bonus regex needs live on the same
    records. The YAML version kept the whole 150 MB parse in memory to reuse it
    for exactly this; streaming makes that unnecessary.
    """
    rows, skills = [], []
    for r in feed.records(z, "types"):
        type_id = int(r["_key"])
        name = feed.en(r.get("name"))
        if not name:
            continue
        rows.append((
            type_id,
            name,
            r.get("groupID"),
            1 if r.get("published", True) else 0,
            r.get("marketGroupID"),
            # Fall back to `volume` for the handful of types that carry no
            # packaged figure; for those two are the same anyway.
            r.get("packagedVolume", r.get("volume")),
        ))
        if type_id in _SKILL_EXCLUDE or r.get("groupID") == _IMPLANT_GROUP:
            continue
        m = _BONUS_RE.search(feed.en(r.get("description")))
        if m:
            skills.append((type_id, name, float(m.group(1))))

    conn.executemany(
        "INSERT OR REPLACE INTO sde_types "
        "(type_id, name, group_id, published, market_group_id, packaged_volume) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.execute("DELETE FROM sde_skill_time_bonus")
    conn.executemany("INSERT OR REPLACE INTO sde_skill_time_bonus VALUES (?,?,?)", skills)
    conn.commit()
    console.print(f"[green]  types: {len(rows):,}[/] "
                  f"[dim]({len(skills)} with a manufacturing/reaction time bonus)[/]")
    return len(rows)


def import_groups(conn: sqlite3.Connection, z: zipfile.ZipFile) -> int:
    """groups -> sde_groups.

    Previously populated once via ESI, which meant new groups (e.g. 5120 Command
    Carrier from Cradle of War) were never backfilled for existing users —
    rig_applies_to_product then returned False through its INNER JOIN and no rig
    applied to products from those groups.
    """
    rows = [(int(r["_key"]), feed.en(r.get("name")) or f"Group {r['_key']}")
            for r in feed.records(z, "groups")]
    conn.executemany("INSERT OR REPLACE INTO sde_groups (group_id, name) VALUES (?,?)", rows)
    conn.commit()
    console.print(f"[green]  groups: {len(rows):,}[/]")
    return len(rows)


def import_blueprints(conn: sqlite3.Connection, z: zipfile.ZipFile) -> int:
    bp_rows, mat_rows, prod_rows, skill_rows = [], [], [], []

    for r in feed.records(z, "blueprints"):
        bp_id = int(r["_key"])
        activities = r.get("activities") or {}

        bp_rows.append((
            bp_id,
            r.get("maxProductionLimit", 1),
            (activities.get("manufacturing") or {}).get("time", 0),
            (activities.get("reaction") or {}).get("time", 0),
        ))

        for activity_name in _ACTIVITIES:
            activity = activities.get(activity_name)
            if not activity:
                continue
            for mat in activity.get("materials") or []:
                mat_rows.append((bp_id, activity_name,
                                 int(mat["typeID"]), int(mat["quantity"])))
            for prod in activity.get("products") or []:
                prod_rows.append((bp_id, activity_name, int(prod["typeID"]),
                                  int(prod.get("quantity", 1)),
                                  float(prod.get("probability", 1.0))))
            for skill in activity.get("skills") or []:
                skill_rows.append((bp_id, activity_name,
                                   int(skill["typeID"]), int(skill.get("level", 1))))

    conn.executemany("INSERT OR REPLACE INTO sde_blueprints VALUES (?,?,?,?)", bp_rows)
    conn.executemany("INSERT OR REPLACE INTO sde_blueprint_materials VALUES (?,?,?,?)", mat_rows)
    conn.executemany("INSERT OR REPLACE INTO sde_blueprint_products VALUES (?,?,?,?,?)", prod_rows)
    conn.execute("DELETE FROM sde_blueprint_skills")
    conn.executemany("INSERT OR REPLACE INTO sde_blueprint_skills VALUES (?,?,?,?)", skill_rows)
    conn.commit()
    console.print(f"[green]  blueprints: {len(bp_rows):,}[/] [dim]({len(mat_rows):,} materials, "
                  f"{len(prod_rows):,} products, {len(skill_rows):,} skills)[/]")
    return len(bp_rows)


def import_planet_schematics(conn: sqlite3.Connection, z: zipfile.ZipFile) -> int:
    """PI factory schematics: inputs -> output (type_ids + quantities) + cycle time.

    Powers the Planets production-chain view and the PI planner. The output is
    the single type whose `isInput` is false; everything else is an input.
    """
    sch_rows, mat_rows = [], []
    for r in feed.records(z, "planetSchematics"):
        sid = int(r["_key"])
        out_id, out_qty = None, 0
        # `types` is a list of {_key, isInput, quantity} -- exactly one entry
        # has isInput false and that is the schematic's output.
        for spec in r.get("types") or []:
            type_id, qty = int(spec["_key"]), int(spec.get("quantity") or 0)
            if spec.get("isInput"):
                mat_rows.append((sid, type_id, qty))
            else:
                out_id, out_qty = type_id, qty
        sch_rows.append((sid, feed.en(r.get("name")), int(r.get("cycleTime") or 0),
                         out_id, out_qty))

    conn.executemany("INSERT OR REPLACE INTO sde_planet_schematics VALUES (?,?,?,?,?)", sch_rows)
    conn.executemany("INSERT OR REPLACE INTO sde_planet_schematic_materials VALUES (?,?,?)", mat_rows)
    conn.commit()
    console.print(f"[green]  planet schematics: {len(sch_rows):,}[/] "
                  f"[dim]({len(mat_rows):,} inputs)[/]")
    return len(sch_rows)


def _progress(done: int, total: int):
    if not total:
        return
    pct = done * 100 // total
    if pct != getattr(_progress, "_last", None):
        _progress._last = pct
        console.print(f"[dim]  downloading… {pct}% ({done/1e6:.0f}/{total/1e6:.0f} MB)[/]",
                      end="\r")


def main():
    ap = argparse.ArgumentParser(description="Import the EVE SDE into SQLite.")
    ap.add_argument("--build", type=int, help="pin to a specific SDE build number")
    ap.add_argument("--zip", help="use an archive already on disk instead of downloading")
    ap.add_argument("--out", default=DB_PATH, help="database to write (default: eve_cache.db)")
    ap.add_argument("--cache", default=CACHE_DIR, help="where downloaded archives are kept")
    ap.add_argument("--fresh", action="store_true",
                    help="delete the target database first (use when building a bundle)")
    args = ap.parse_args()

    console.print("[bold]EVE Retroindustry — import SDE[/]\n")

    if args.zip:
        archive = args.zip
        build = feed.archive_build(archive)
        if build is None:
            console.print(f"[red]Not a readable SDE archive: {archive}[/]")
            return 1
        console.print(f"Using local archive [cyan]{archive}[/] (build {build})")
    else:
        build = feed.Build(args.build) if args.build else feed.latest_build()
        console.print(f"Build [cyan]{build}[/]")
        try:
            changed = feed.schema_changed_datasets(build.number)
        except Exception:
            changed = set()          # advisory only; never block an import on it
        if changed:
            console.print(f"[yellow]  schema changed this build: {', '.join(sorted(changed))}[/]")
        archive = feed.download_archive(build.number, args.cache, progress=_progress)
        console.print(f"Archive [cyan]{archive}[/]" + " " * 30)

    if args.fresh and os.path.exists(args.out):
        os.remove(args.out)
        console.print(f"[dim]Removed existing {args.out}[/]")

    t0 = time.time()
    conn = sqlite3.connect(args.out)
    try:
        init_db(conn)
        with zipfile.ZipFile(archive) as z:
            import_types(conn, z)
            import_groups(conn, z)
            import_blueprints(conn, z)
            import_planet_schematics(conn, z)
        record_build(conn, build)
    finally:
        conn.close()

    console.print(f"\n[bold green]Done in {time.time()-t0:.1f}s[/] -> {args.out}")

    # Smoke check — a capital that exercises the whole join path.
    conn = sqlite3.connect(args.out)
    try:
        bp = conn.execute(
            "SELECT blueprint_type_id FROM sde_blueprint_products "
            "WHERE product_type_id=? AND activity='manufacturing'", (24483,)).fetchone()
        if not bp:
            console.print("[red]Smoke check failed: no blueprint for Nidhoggur (24483)[/]")
            return 1
        mats = conn.execute("""
            SELECT t.name, m.quantity FROM sde_blueprint_materials m
            JOIN sde_types t ON t.type_id = m.material_type_id
            WHERE m.blueprint_type_id=? AND m.activity='manufacturing'
            ORDER BY m.quantity DESC""", (bp[0],)).fetchall()
        console.print(f"\n[bold]Nidhoggur[/] — {len(mats)} materials")
        for name, qty in mats:
            console.print(f"    {name}: {qty:,}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
