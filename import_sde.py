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
from app.db.schema import apply_sde_schema

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

# Activities imported from each blueprint.
#
# `invention` lives on the **T1** blueprint and its products are T2 *blueprints*
# carrying a success `probability` — so one T1 BP can invent several T2s (Condor
# invents both the Crow and the Raptor). `copying` is here because inventing
# consumes a T1 BPC run, so its cost is part of an invention attempt.
#
# The two research activities are in the source and still unread; they land with
# the research planner. Adding a name here is all the importer needs.
_ACTIVITIES = ("manufacturing", "reaction", "invention", "copying")


def init_db(conn: sqlite3.Connection):
    """Create the static-data tables and their indexes.

    The DDL used to live here as one long executescript. It moved to
    app/db/schema.py so that one file describes the whole database and so the
    same declaration can emit Postgres DDL — the importer no longer owns a
    second, divergent copy of the schema.
    """
    apply_sde_schema(conn)


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
            r.get("portionSize") or 1,
        ))
        if type_id in _SKILL_EXCLUDE or r.get("groupID") == _IMPLANT_GROUP:
            continue
        m = _BONUS_RE.search(feed.en(r.get("description")))
        if m:
            skills.append((type_id, name, float(m.group(1))))

    conn.executemany(
        "INSERT INTO sde_types "
        "(type_id, name, group_id, published, market_group_id, packaged_volume, "
        "portion_size) VALUES (?,?,?,?,?,?,?) ON CONFLICT (type_id) DO UPDATE SET name=excluded.name, group_id=excluded.group_id, published=excluded.published, market_group_id=excluded.market_group_id, packaged_volume=excluded.packaged_volume, portion_size=excluded.portion_size", rows)
    conn.execute("DELETE FROM sde_skill_time_bonus")
    conn.executemany("INSERT INTO sde_skill_time_bonus VALUES (?,?,?) ON CONFLICT (skill_type_id) DO UPDATE SET skill_name=excluded.skill_name, time_bonus_pct=excluded.time_bonus_pct", skills)
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
    conn.executemany("INSERT INTO sde_groups (group_id, name) VALUES (?,?) ON CONFLICT (group_id) DO UPDATE SET name=excluded.name", rows)
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

    conn.executemany("INSERT INTO sde_blueprints VALUES (?,?,?,?) ON CONFLICT (blueprint_type_id) DO UPDATE SET max_production_limit=excluded.max_production_limit, manufacturing_time=excluded.manufacturing_time, reaction_time=excluded.reaction_time", bp_rows)
    conn.executemany("INSERT INTO sde_blueprint_materials VALUES (?,?,?,?) ON CONFLICT (blueprint_type_id, activity, material_type_id) DO UPDATE SET quantity=excluded.quantity", mat_rows)
    conn.executemany("INSERT INTO sde_blueprint_products VALUES (?,?,?,?,?) ON CONFLICT (blueprint_type_id, activity, product_type_id) DO UPDATE SET quantity=excluded.quantity, probability=excluded.probability", prod_rows)
    conn.execute("DELETE FROM sde_blueprint_skills")
    conn.executemany("INSERT INTO sde_blueprint_skills VALUES (?,?,?,?) ON CONFLICT (blueprint_type_id, activity, skill_type_id) DO UPDATE SET required_level=excluded.required_level", skill_rows)
    conn.commit()
    console.print(f"[green]  blueprints: {len(bp_rows):,}[/] [dim]({len(mat_rows):,} materials, "
                  f"{len(prod_rows):,} products, {len(skill_rows):,} skills)[/]")
    return len(bp_rows)


# Dogma attributes describing a decryptor. CCP's spelling of 1112 is theirs.
_DECRYPTOR_ATTRS = {
    1112: "probability_mult",     # inventionPropabilityMultiplier
    1113: "me_modifier",          # inventionMEModifier
    1114: "te_modifier",          # inventionTEModifier
    1124: "run_modifier",         # inventionMaxRunModifier
}


_DATACORE_GROUP = 333       # the only group ever consumed by an invention job
_REQUIRED_SKILL_1 = 182     # dogma attribute naming a type's primary skill
_DATACORE_PREFIX = "Datacore - "
_SKILL_GROUPS = {270, 268}  # Science, Production — where invention skills live


def import_dogma(conn: sqlite3.Connection, z: zipfile.ZipFile) -> tuple[int, int]:
    """Decryptor effects and datacore-to-skill links, in one pass over typeDogma.

    A decryptor is any type carrying `inventionPropabilityMultiplier` (1112).
    The Subsystems Data Interfaces come along too — they carry the attribute at
    1.0/0/0/0, so they are neutral and harmless to keep.

    A datacore's `requiredSkill1` is the science skill that raises the odds of
    the invention jobs consuming it. Reading it here is what makes the link
    authoritative rather than a string match on names that do not agree.
    """
    names, datacore_ids, skill_ids_by_name = {}, set(), {}
    for r in feed.records(z, "types"):
        name = feed.en(r.get("name"))
        names[r["_key"]] = name
        if r.get("groupID") == _DATACORE_GROUP:
            datacore_ids.add(r["_key"])
        elif r.get("groupID") in _SKILL_GROUPS and name:
            skill_ids_by_name[name] = r["_key"]

    decryptors, links = [], {}
    for r in feed.records(z, "typeDogma"):
        type_id = int(r["_key"])
        attrs = {d["attributeID"]: d["value"] for d in (r.get("dogmaAttributes") or [])}
        if 1112 in attrs:
            vals = {field: float(attrs.get(attr_id) or 0.0)
                    for attr_id, field in _DECRYPTOR_ATTRS.items()}
            decryptors.append((type_id, names.get(type_id, "?"),
                               vals["probability_mult"], vals["me_modifier"],
                               vals["te_modifier"], vals["run_modifier"]))
        if type_id in datacore_ids and attrs.get(_REQUIRED_SKILL_1):
            links[type_id] = int(attrs[_REQUIRED_SKILL_1])

    # `Datacore - Triglavian Quantum Engineering` has no dogma record at all, so
    # nothing declares its skill. Its name happens to match the skill exactly,
    # which is the safe direction to fall back in: dogma is authoritative where
    # it exists (and disagrees with the name for the Amarr and Gallente lines),
    # and the name is only consulted where dogma says nothing.
    for type_id in datacore_ids - set(links):
        bare = names.get(type_id, "").replace(_DATACORE_PREFIX, "").strip()
        if bare in skill_ids_by_name:
            links[type_id] = skill_ids_by_name[bare]

    conn.executemany("INSERT INTO sde_decryptors VALUES (?,?,?,?,?,?) ON CONFLICT (type_id) DO UPDATE SET name=excluded.name, probability_mult=excluded.probability_mult, me_modifier=excluded.me_modifier, te_modifier=excluded.te_modifier, run_modifier=excluded.run_modifier", decryptors)
    conn.executemany("INSERT INTO sde_datacore_skills VALUES (?,?) ON CONFLICT (type_id) DO UPDATE SET skill_type_id=excluded.skill_type_id",
                     sorted(links.items()))
    conn.commit()
    console.print(f"[green]  decryptors: {len(decryptors)}[/] "
                  f"[dim]({len(links)} datacore skill links)[/]")
    return len(decryptors), len(links)


def import_type_materials(conn: sqlite3.Connection, z: zipfile.ZipFile) -> int:
    """typeMaterials -> sde_type_materials: what a batch reprocesses into.

    Powers the refine calculator, ore valuation and the mining ledger. Note the
    quantities are per *batch* of `portion_size`, so anything reporting a
    per-unit or per-m3 figure has to divide.
    """
    rows = [
        (int(r["_key"]), int(m["materialTypeID"]), int(m["quantity"]))
        for r in feed.records(z, "typeMaterials")
        for m in (r.get("materials") or [])
    ]
    conn.executemany("INSERT INTO sde_type_materials VALUES (?,?,?) ON CONFLICT (type_id, material_type_id) DO UPDATE SET quantity=excluded.quantity", rows)
    conn.commit()
    console.print(f"[green]  type materials: {len(rows):,}[/] "
                  f"[dim](reprocessing yields)[/]")
    return len(rows)


def import_market_groups(conn: sqlite3.Connection, z: zipfile.ZipFile) -> int:
    """marketGroups -> sde_market_groups: the in-game market tree.

    `sde_types.market_group_id` already points into this; until now there was
    nothing to point at, so the Prices page could only be one flat list.
    """
    rows = [
        (int(r["_key"]), r.get("parentGroupID"), feed.en(r.get("name")) or f"Group {r['_key']}",
         1 if r.get("hasTypes") else 0, r.get("iconID"))
        for r in feed.records(z, "marketGroups")
    ]
    conn.executemany("INSERT INTO sde_market_groups VALUES (?,?,?,?,?) ON CONFLICT (market_group_id) DO UPDATE SET parent_group_id=excluded.parent_group_id, name=excluded.name, has_types=excluded.has_types, icon_id=excluded.icon_id", rows)
    conn.commit()
    roots = sum(1 for r in rows if r[1] is None)
    console.print(f"[green]  market groups: {len(rows):,}[/] [dim]({roots} roots)[/]")
    return len(rows)


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

    conn.executemany("INSERT INTO sde_planet_schematics VALUES (?,?,?,?,?) ON CONFLICT (schematic_id) DO UPDATE SET name=excluded.name, cycle_time=excluded.cycle_time, output_type_id=excluded.output_type_id, output_qty=excluded.output_qty", sch_rows)
    conn.executemany("INSERT INTO sde_planet_schematic_materials VALUES (?,?,?) ON CONFLICT (schematic_id, type_id) DO UPDATE SET quantity=excluded.quantity", mat_rows)
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
            import_dogma(conn, z)
            import_type_materials(conn, z)
            import_market_groups(conn, z)
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
