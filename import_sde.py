"""
Import the EVE Online static data export into the application's database.

Writes through SQLAlchemy, so `--out` accepts a SQLite path *or* a database
URL and the same import lands on either backend. It used to open
`sqlite3.connect()` directly, which meant the SDE tables simply did not exist
on Postgres — and six of the app's statements JOIN `sde_types` to runtime
tables, so no page could render there.

Reads CCP's **JSONL** export, downloaded straight from their static-data service
and pinned to a build number — see `app/sde/feed.py` for why. The previous
version parsed YAML out of a hand-populated `data/` directory using PyYAML,
which is not in `requirements.txt`; that is why the dev-setup doc told you not
to run this script. It is now safe to run.

Usage:
    python import_sde.py                      # newest build -> the app's database
    python import_sde.py --out sde_base.db    # a SQLite file, for the test fixture
    python import_sde.py --build 3470007      # pin to a specific build
    python import_sde.py --zip some.zip       # use an archive already on disk
    EVE_DATABASE_URL=postgresql+psycopg://... python import_sde.py

The default target is whatever `EVE_DATABASE_URL`/`EVE_APP_DIR` say the
application uses, not a fixed file beside this script. Those coincided in the
documented deployment and diverge the moment the data directory is not the
checkout — in which case the old default imported the SDE into a database
nothing ever read.
"""
from __future__ import annotations

import argparse
import os
import re
import time
import zipfile

from rich.console import Console
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

from app.sde import feed
from app.db.location import database_url
from app.db.schema import SDE_TABLES, create_sde_schema, metadata

# Matches "1% reduction in manufacturing time" or "...in reaction time".
# Reactions skill (45746) has "...reaction time per skill level" — without
# this alternation it would be silently dropped from sde_skill_time_bonus.
_BONUS_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*%\s*reduction\s+in\s+(?:manufacturing|reaction)\s+time',
    re.IGNORECASE,
)

console = Console()

_ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_ROOT, "data", "sde-archives")


def _target_url(out: str | None) -> str:
    """The database to import into, as a SQLAlchemy URL.

    `--out` is still a file path, because that is what building the test
    fixture wants (`--out sde_base.db`). Anything carrying a scheme is passed
    through untouched, so `--out postgresql+psycopg://...` works and so does
    leaving it off entirely — the default is the database the *application*
    reads, resolved the same way the app resolves it.

    That default used to be a fixed `eve_cache.db` beside this script. It
    agreed with the app only while the data directory happened to be the
    checkout; set `EVE_APP_DIR` anywhere else and the import landed in a
    database nothing opened, with no error to say so.
    """
    if not out:
        return database_url()
    if "://" in out:
        return out
    return f"sqlite:///{os.path.abspath(out)}"

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


def init_db(conn: Connection):
    """Create the static-data tables and their indexes.

    The DDL used to live here as one long executescript. It moved to
    app/db/schema.py so that one file describes the whole database and so the
    same declaration can emit Postgres DDL — the importer no longer owns a
    second, divergent copy of the schema.
    """
    create_sde_schema(conn)
    conn.commit()


def _upsert(conn: Connection, table: str):
    """`INSERT ... ON CONFLICT (pk) DO UPDATE SET <every other column>`.

    Every write in this file has that exact shape: re-importing a build must
    overwrite what the last one wrote, and the primary key is the identity CCP
    already assigned. So the statement is derived from the declared table
    rather than written out fourteen times.

    Two things this buys beyond portability. The conflict target comes from the
    real primary key, so it cannot drift from the schema. And the column list
    is named — several of these statements used to be `INSERT INTO t VALUES
    (?,?,?)`, which is correct only for as long as nobody inserts a column in
    the middle of the declaration.

    `on_conflict_do_update` is dialect-specific in SQLAlchemy: the two
    constructs take the same arguments but must be imported from the dialect
    that is actually underneath, so the choice is made here from the live
    connection rather than from a module-level guess about the deployment.
    """
    t = metadata.tables[table]
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    stmt = insert(t)
    pk = {c.name for c in t.primary_key.columns}
    rest = [c.name for c in t.columns if c.name not in pk]
    if not rest:
        # Key-only table: the row's existence is the whole of its content.
        return stmt.on_conflict_do_nothing(index_elements=sorted(pk))
    return stmt.on_conflict_do_update(
        index_elements=[c.name for c in t.primary_key.columns],
        set_={name: getattr(stmt.excluded, name) for name in rest},
    )


def _write(conn: Connection, table: str, rows: list[dict]) -> None:
    """Upsert `rows` into `table`. A no-op on an empty list.

    SQLAlchemy raises on `execute(stmt, [])` rather than treating it as zero
    work, and a miniature archive — or a dataset CCP has emptied — legitimately
    produces none.
    """
    if not rows:
        return
    conn.execute(_upsert(conn, table), rows)


def record_build(conn: Connection, build: feed.Build):
    _write(conn, "sde_build", [{"id": 1, "build_number": build.number,
                                "release_date": build.release_date,
                                "imported_at": time.time()}])
    conn.commit()


def import_types(conn: Connection, z: zipfile.ZipFile) -> int:
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
        rows.append({
            "type_id": type_id,
            "name": name,
            "group_id": r.get("groupID"),
            "published": 1 if r.get("published", True) else 0,
            "market_group_id": r.get("marketGroupID"),
            # Fall back to `volume` for the handful of types that carry no
            # packaged figure; for those two are the same anyway.
            "packaged_volume": r.get("packagedVolume", r.get("volume")),
            "portion_size": r.get("portionSize") or 1,
        })
        if type_id in _SKILL_EXCLUDE or r.get("groupID") == _IMPLANT_GROUP:
            continue
        m = _BONUS_RE.search(feed.en(r.get("description")))
        if m:
            skills.append({"skill_type_id": type_id, "skill_name": name,
                           "time_bonus_pct": float(m.group(1))})

    _write(conn, "sde_types", rows)
    conn.execute(text("DELETE FROM sde_skill_time_bonus"))
    _write(conn, "sde_skill_time_bonus", skills)
    conn.commit()
    console.print(f"[green]  types: {len(rows):,}[/] "
                  f"[dim]({len(skills)} with a manufacturing/reaction time bonus)[/]")
    return len(rows)


def import_groups(conn: Connection, z: zipfile.ZipFile) -> int:
    """groups -> sde_groups.

    Previously populated once via ESI, which meant new groups (e.g. 5120 Command
    Carrier from Cradle of War) were never backfilled for existing users —
    rig_applies_to_product then returned False through its INNER JOIN and no rig
    applied to products from those groups.
    """
    rows = [{"group_id": int(r["_key"]),
             "name": feed.en(r.get("name")) or f"Group {r['_key']}"}
            for r in feed.records(z, "groups")]
    _write(conn, "sde_groups", rows)
    conn.commit()
    console.print(f"[green]  groups: {len(rows):,}[/]")
    return len(rows)


def import_blueprints(conn: Connection, z: zipfile.ZipFile) -> int:
    bp_rows, mat_rows, prod_rows, skill_rows = [], [], [], []

    for r in feed.records(z, "blueprints"):
        bp_id = int(r["_key"])
        activities = r.get("activities") or {}

        bp_rows.append({
            "blueprint_type_id": bp_id,
            "max_production_limit": r.get("maxProductionLimit", 1),
            "manufacturing_time": (activities.get("manufacturing") or {}).get("time", 0),
            "reaction_time": (activities.get("reaction") or {}).get("time", 0),
        })

        for activity_name in _ACTIVITIES:
            activity = activities.get(activity_name)
            if not activity:
                continue
            for mat in activity.get("materials") or []:
                mat_rows.append({"blueprint_type_id": bp_id, "activity": activity_name,
                                 "material_type_id": int(mat["typeID"]),
                                 "quantity": int(mat["quantity"])})
            for prod in activity.get("products") or []:
                prod_rows.append({"blueprint_type_id": bp_id, "activity": activity_name,
                                  "product_type_id": int(prod["typeID"]),
                                  "quantity": int(prod.get("quantity", 1)),
                                  "probability": float(prod.get("probability", 1.0))})
            for skill in activity.get("skills") or []:
                skill_rows.append({"blueprint_type_id": bp_id, "activity": activity_name,
                                   "skill_type_id": int(skill["typeID"]),
                                   "required_level": int(skill.get("level", 1))})

    _write(conn, "sde_blueprints", bp_rows)
    _write(conn, "sde_blueprint_materials", mat_rows)
    _write(conn, "sde_blueprint_products", prod_rows)
    conn.execute(text("DELETE FROM sde_blueprint_skills"))
    _write(conn, "sde_blueprint_skills", skill_rows)
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


def import_dogma(conn: Connection, z: zipfile.ZipFile) -> tuple[int, int]:
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
            decryptors.append({"type_id": type_id,
                               "name": names.get(type_id, "?"), **vals})
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

    _write(conn, "sde_decryptors", decryptors)
    _write(conn, "sde_datacore_skills",
           [{"type_id": t, "skill_type_id": s} for t, s in sorted(links.items())])
    conn.commit()
    console.print(f"[green]  decryptors: {len(decryptors)}[/] "
                  f"[dim]({len(links)} datacore skill links)[/]")
    return len(decryptors), len(links)


def import_type_materials(conn: Connection, z: zipfile.ZipFile) -> int:
    """typeMaterials -> sde_type_materials: what a batch reprocesses into.

    Powers the refine calculator, ore valuation and the mining ledger. Note the
    quantities are per *batch* of `portion_size`, so anything reporting a
    per-unit or per-m3 figure has to divide.
    """
    rows = [
        {"type_id": int(r["_key"]), "material_type_id": int(m["materialTypeID"]),
         "quantity": int(m["quantity"])}
        for r in feed.records(z, "typeMaterials")
        for m in (r.get("materials") or [])
    ]
    _write(conn, "sde_type_materials", rows)
    conn.commit()
    console.print(f"[green]  type materials: {len(rows):,}[/] "
                  f"[dim](reprocessing yields)[/]")
    return len(rows)


def import_market_groups(conn: Connection, z: zipfile.ZipFile) -> int:
    """marketGroups -> sde_market_groups: the in-game market tree.

    `sde_types.market_group_id` already points into this; until now there was
    nothing to point at, so the Prices page could only be one flat list.
    """
    rows = [
        {"market_group_id": int(r["_key"]), "parent_group_id": r.get("parentGroupID"),
         "name": feed.en(r.get("name")) or f"Group {r['_key']}",
         "has_types": 1 if r.get("hasTypes") else 0, "icon_id": r.get("iconID")}
        for r in feed.records(z, "marketGroups")
    ]
    _write(conn, "sde_market_groups", rows)
    conn.commit()
    roots = sum(1 for r in rows if r["parent_group_id"] is None)
    console.print(f"[green]  market groups: {len(rows):,}[/] [dim]({roots} roots)[/]")
    return len(rows)


def import_planet_schematics(conn: Connection, z: zipfile.ZipFile) -> int:
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
                mat_rows.append({"schematic_id": sid, "type_id": type_id,
                                 "quantity": qty})
            else:
                out_id, out_qty = type_id, qty
        sch_rows.append({"schematic_id": sid, "name": feed.en(r.get("name")),
                         "cycle_time": int(r.get("cycleTime") or 0),
                         "output_type_id": out_id, "output_qty": out_qty})

    _write(conn, "sde_planet_schematics", sch_rows)
    _write(conn, "sde_planet_schematic_materials", mat_rows)
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


def _display(url: str) -> str:
    """A URL safe to print. `make_url` masks the password; a path prints whole."""
    if url.startswith("sqlite"):
        return make_url(url).database or url
    return make_url(url).render_as_string(hide_password=True)


def _drop_static_data(engine, url: str) -> None:
    """`--fresh`: throw the existing static data away before importing.

    For a SQLite *file* this deletes the file, which is what building
    `sde_base.db` wants — a bundle with nothing else in it.

    For any other target it drops the SDE tables only. Deleting the database
    would take `characters` and every refresh token with it, which is a defect
    this project has shipped once already, from a different button.
    """
    if url.startswith("sqlite"):
        path = make_url(url).database
        if path and os.path.exists(path):
            engine.dispose()
            os.remove(path)
            console.print(f"[dim]Removed existing {path}[/]")
        return
    tables = [metadata.tables[n] for n in sorted(SDE_TABLES)]
    metadata.drop_all(engine, tables=tables, checkfirst=True)
    console.print(f"[dim]Dropped {len(tables)} static-data tables[/]")


def main():
    ap = argparse.ArgumentParser(
        description="Import the EVE SDE into the application's database.")
    ap.add_argument("--build", type=int, help="pin to a specific SDE build number")
    ap.add_argument("--zip", help="use an archive already on disk instead of downloading")
    ap.add_argument("--out", default=None,
                    help="SQLite path or database URL (default: the app's database)")
    ap.add_argument("--cache", default=CACHE_DIR, help="where downloaded archives are kept")
    ap.add_argument("--fresh", action="store_true",
                    help="start the static data from scratch (use when building a bundle)")
    args = ap.parse_args()
    url = _target_url(args.out)

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

    engine = create_engine(url)

    if args.fresh:
        _drop_static_data(engine, url)

    t0 = time.time()
    with engine.connect() as conn:
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

    console.print(f"\n[bold green]Done in {time.time()-t0:.1f}s[/] -> {_display(url)}")

    # Smoke check — a capital that exercises the whole join path.
    with engine.connect() as conn:
        bp = conn.execute(text(
            "SELECT blueprint_type_id FROM sde_blueprint_products "
            "WHERE product_type_id = :tid AND activity = 'manufacturing'"),
            {"tid": 24483}).fetchone()
        if not bp:
            console.print("[red]Smoke check failed: no blueprint for Nidhoggur (24483)[/]")
            return 1
        mats = conn.execute(text("""
            SELECT t.name, m.quantity FROM sde_blueprint_materials m
            JOIN sde_types t ON t.type_id = m.material_type_id
            WHERE m.blueprint_type_id = :bp AND m.activity = 'manufacturing'
            ORDER BY m.quantity DESC"""), {"bp": bp[0]}).fetchall()
        console.print(f"\n[bold]Nidhoggur[/] — {len(mats)} materials")
        for name, qty in mats:
            console.print(f"    {name}: {qty:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
