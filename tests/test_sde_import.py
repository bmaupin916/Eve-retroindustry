"""The JSONL SDE importer.

Hermetic: every test builds a miniature SDE archive in a temp directory, so
nothing here downloads the real 95 MB export or touches the network.

Two of these cover mistakes made while writing the importer, both of which
would have shipped silently:

* `planetSchematics.types` is a **list** of records carrying `_key`, not a dict
  keyed by type id. Treating it as a dict raised on the real data.
* The tables have to keep the column names the app already queries
  (`required_level`, `time_bonus_pct`). A fresh database built with plausible
  but different names passes every import check and then breaks the app.
"""
from __future__ import annotations

import json
import os
import sqlite3
import zipfile

import pytest
from sqlalchemy import create_engine

from app.sde import feed

BUILD = 3470007


def _archive(path: str, datasets: dict[str, list[dict]], build: int = BUILD) -> str:
    """Write a miniature SDE archive."""
    datasets = dict(datasets)
    datasets.setdefault("_sde", [{"_key": "sde", "buildNumber": build,
                                  "releaseDate": "2026-08-17T11:26:56Z"}])
    with zipfile.ZipFile(path, "w") as z:
        for name, records in datasets.items():
            z.writestr(f"{name}.jsonl",
                       "\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def tiny(tmp_path):
    return _archive(str(tmp_path / "sde.zip"), {
        "types": [
            # Packaged and assembled volume are equal for a mineral...
            {"_key": 34, "name": {"en": "Tritanium"}, "groupID": 18,
             "published": True, "marketGroupID": 1857,
             "volume": 0.01, "packagedVolume": 0.01},
            # ...and wildly different for a capital ship.
            {"_key": 24483, "name": {"en": "Nidhoggur"}, "groupID": 547,
             "published": True, "marketGroupID": 1376,
             "volume": 11250000.0, "packagedVolume": 1300000.0},
            # A skill whose description carries a time bonus.
            {"_key": 3395, "name": {"en": "Industry"}, "groupID": 268,
             "description": {"en": "4% reduction in manufacturing time per level."}},
            # No English name -> skipped rather than imported as blank.
            {"_key": 999999, "name": {"de": "Nur Deutsch"}, "groupID": 1},
        ],
        "groups": [{"_key": 18, "name": {"en": "Mineral"}},
                   {"_key": 547, "name": {"en": "Carrier"}}],
        "blueprints": [{
            "_key": 24484, "maxProductionLimit": 1,
            "activities": {
                "manufacturing": {
                    "materials": [{"typeID": 34, "quantity": 1000}],
                    "products": [{"typeID": 24483, "quantity": 1}],
                    "skills": [{"typeID": 3395, "level": 5}],
                    "time": 6000,
                },
                # Present in the source, deliberately not imported yet.
                "invention": {
                    "materials": [{"typeID": 20416, "quantity": 2}],
                    "products": [{"typeID": 12345, "quantity": 1,
                                  "probability": 0.3}],
                    "time": 63900,
                },
            },
        }],
        "planetSchematics": [{
            "_key": 65, "cycleTime": 3600, "name": {"en": "Superconductors"},
            "types": [{"_key": 2389, "isInput": True, "quantity": 40},
                      {"_key": 3645, "isInput": True, "quantity": 40},
                      {"_key": 9838, "isInput": False, "quantity": 5}],
        }],
    })


def _import(archive: str, db: str, steps) -> sqlite3.Connection:
    """Run `steps` of the importer into `db`, then hand back a reader.

    The importer writes through SQLAlchemy now, so it can target Postgres. The
    assertions below are about what the SDE *contains* rather than how it is
    addressed, so they keep reading over plain `sqlite3` — a separate
    connection, opened after the import has committed.

    Portability is asserted in `tests/test_sde_on_postgres.py`, which runs the
    same import twice, once per backend. Doing it here as well would double the
    cost of every content assertion to re-prove one thing.
    """
    import import_sde

    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        import_sde.init_db(conn)
        with zipfile.ZipFile(archive) as z:
            for step in steps:
                getattr(import_sde, step)(conn, z)
        import_sde.record_build(conn, feed.Build(BUILD, "2026-08-17T11:26:56Z"))
    engine.dispose()
    return sqlite3.connect(db)


@pytest.fixture
def imported(tiny, tmp_path):
    return _import(tiny, str(tmp_path / "out.db"), (
        "import_types", "import_groups", "import_blueprints",
        "import_planet_schematics",
    ))


# ── reading the archive ────────────────────────────────────────────────────
def test_archive_build_reads_the_stamp(tiny):
    build = feed.archive_build(tiny)
    assert build.number == BUILD
    assert build.release_date == "2026-08-17T11:26:56Z"


def test_a_corrupt_archive_is_not_trusted(tmp_path):
    """verify_archive doubles as the integrity check, so garbage must come back
    as None rather than raising out of a download."""
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip file")
    assert feed.archive_build(str(bad)) is None
    assert feed.verify_archive(str(bad)) is None
    assert feed.verify_archive(str(tmp_path / "does-not-exist.zip")) is None


def test_a_missing_dataset_yields_nothing_rather_than_raising(tiny):
    """A build that drops a file should degrade to importing nothing for it."""
    assert list(feed.records(tiny, "somethingRemovedByCCP")) == []


def test_datasets_lists_names_without_the_suffix(tiny):
    assert "types" in feed.datasets(tiny)
    assert not any(n.endswith(".jsonl") for n in feed.datasets(tiny))


def test_en_tolerates_every_shape_the_sde_has_used():
    assert feed.en({"en": "Tritanium", "de": "Tritanium"}) == "Tritanium"
    assert feed.en({"de": "Nur Deutsch"}) == ""      # no English -> empty, not KeyError
    assert feed.en("bare string") == "bare string"   # older exports
    assert feed.en(None) == ""


# ── the import ─────────────────────────────────────────────────────────────
def test_packaged_volume_is_stored_not_assembled_volume(imported):
    """The bug this rename fixed. An assembled Nidhoggur is 11,250,000 m³ and a
    packaged one is 1,300,000 — and profit-per-m³ is about what a hauler
    carries, so the packaged figure is the only correct one."""
    vols = dict(imported.execute(
        "SELECT type_id, packaged_volume FROM sde_types WHERE type_id IN (34, 24483)"))
    assert vols[24483] == 1_300_000.0
    assert vols[34] == 0.01


def test_types_without_an_english_name_are_skipped(imported):
    assert imported.execute(
        "SELECT COUNT(*) FROM sde_types WHERE type_id=999999").fetchone()[0] == 0


def test_skill_time_bonuses_come_from_the_same_pass(imported):
    row = imported.execute(
        "SELECT skill_name, time_bonus_pct FROM sde_skill_time_bonus "
        "WHERE skill_type_id=3395").fetchone()
    assert row == ("Industry", 4.0)


def test_planet_schematic_types_are_a_list_not_a_dict(imported):
    """`types` is a list of {_key, isInput, quantity}. Reading it as a dict
    raised on the real data; the output is the one entry with isInput false."""
    assert imported.execute(
        "SELECT output_type_id, output_qty, cycle_time FROM sde_planet_schematics "
        "WHERE schematic_id=65").fetchone() == (9838, 5, 3600)
    inputs = dict(imported.execute(
        "SELECT type_id, quantity FROM sde_planet_schematic_materials "
        "WHERE schematic_id=65"))
    assert inputs == {2389: 40, 3645: 40}


def test_column_names_match_what_the_app_queries(imported):
    """A fresh database has to keep the names already in the app's SQL.
    Plausible alternatives (`level`, `bonus_pct`) import fine and then break
    every query that names them."""
    def cols(table):
        return {r[1] for r in imported.execute(f"PRAGMA table_info({table})")}
    assert "required_level" in cols("sde_blueprint_skills")
    assert {"skill_name", "time_bonus_pct"} <= cols("sde_skill_time_bonus")
    assert "packaged_volume" in cols("sde_types")
    assert "volume" not in cols("sde_types")


def test_invention_is_imported_with_its_probability(imported):
    """Invention lives on the T1 blueprint and its product is the T2
    *blueprint*, carrying the success chance. Without this every T2 figure in
    the app treats the blueprint as free."""
    acts = {r[0] for r in imported.execute(
        "SELECT DISTINCT activity FROM sde_blueprint_materials")}
    assert acts == {"manufacturing", "invention"}
    assert imported.execute(
        "SELECT product_type_id, quantity, probability FROM sde_blueprint_products "
        "WHERE activity='invention'").fetchone() == (12345, 1, 0.3)
    # The datacores, on the T1 blueprint.
    assert imported.execute(
        "SELECT material_type_id, quantity FROM sde_blueprint_materials "
        "WHERE activity='invention'").fetchone() == (20416, 2)


def test_research_activities_are_still_unread(imported):
    """The two research activities are in the source and land with the research
    planner. Pinned so enabling them is deliberate, not a surprise."""
    acts = {r[0] for r in imported.execute(
        "SELECT DISTINCT activity FROM sde_blueprint_materials")}
    assert "research_material" not in acts
    assert "research_time" not in acts


def test_the_build_is_recorded(imported):
    assert imported.execute(
        "SELECT build_number, release_date FROM sde_build WHERE id=1"
    ).fetchone() == (BUILD, "2026-08-17T11:26:56Z")


# ── reprocessing yields and the market tree ────────────────────────────────
@pytest.fixture
def ore_archive(tmp_path):
    """Plagioclase and Glacial Mass, with the real numbers."""
    return _archive(str(tmp_path / "ore.zip"), {
        "types": [
            {"_key": 18, "name": {"en": "Plagioclase"}, "groupID": 456,
             "published": True, "volume": 0.35, "packagedVolume": 0.35,
             "portionSize": 100, "marketGroupID": 517},
            {"_key": 16262, "name": {"en": "Glacial Mass"}, "groupID": 465,
             "published": True, "volume": 1000.0, "packagedVolume": 1000.0,
             "portionSize": 1},
            {"_key": 34, "name": {"en": "Tritanium"}, "groupID": 18, "published": True},
            {"_key": 36, "name": {"en": "Mexallon"}, "groupID": 18, "published": True},
        ],
        "typeMaterials": [
            {"_key": 18, "materials": [{"materialTypeID": 34, "quantity": 175},
                                       {"materialTypeID": 36, "quantity": 70}]},
        ],
        "marketGroups": [
            {"_key": 4, "name": {"en": "Ships"}, "hasTypes": False},
            {"_key": 1361, "name": {"en": "Battleships"}, "parentGroupID": 4,
             "hasTypes": False},
            {"_key": 517, "name": {"en": "Standard Battleships"},
             "parentGroupID": 1361, "hasTypes": True},
        ],
    })


@pytest.fixture
def ore_db(ore_archive, tmp_path):
    return _import(ore_archive, str(tmp_path / "ore.db"), (
        "import_types", "import_type_materials", "import_market_groups",
    ))


def test_portion_size_is_the_reprocessing_batch(ore_db):
    """Ore refines 100 at a time, ice 1 at a time. Getting this wrong scales
    every yield by 100."""
    sizes = dict(ore_db.execute(
        "SELECT type_id, portion_size FROM sde_types WHERE type_id IN (18, 16262)"))
    assert sizes[18] == 100          # Plagioclase
    assert sizes[16262] == 1         # Glacial Mass, an ice


def test_yields_are_per_batch_and_match_the_published_figures(ore_db):
    """Plagioclase is the cross-check fixture: 0.35 m³, 100 per batch, 175
    Tritanium and 70 Mexallon, which is 5.0 and 2.0 per m³ — the numbers both
    ore.cerlestes.de and the DARK mining spreadsheet report."""
    vol, portion = ore_db.execute(
        "SELECT packaged_volume, portion_size FROM sde_types WHERE type_id=18").fetchone()
    yields = dict(ore_db.execute(
        "SELECT material_type_id, quantity FROM sde_type_materials WHERE type_id=18"))
    assert yields == {34: 175, 36: 70}
    assert yields[34] / (vol * portion) == pytest.approx(5.0)
    assert yields[36] / (vol * portion) == pytest.approx(2.0)


def test_market_groups_form_a_walkable_tree(ore_db):
    """The hierarchy the Prices rebuild needs: a root with no parent, interior
    nodes, and leaves flagged as the ones that hold items."""
    root = ore_db.execute(
        "SELECT market_group_id, name FROM sde_market_groups "
        "WHERE parent_group_id IS NULL").fetchone()
    assert root == (4, "Ships")

    child = ore_db.execute(
        "SELECT market_group_id, name, has_types FROM sde_market_groups "
        "WHERE parent_group_id=4").fetchone()
    assert child == (1361, "Battleships", 0)

    leaf = ore_db.execute(
        "SELECT market_group_id, name, has_types FROM sde_market_groups "
        "WHERE parent_group_id=1361").fetchone()
    assert leaf == (517, "Standard Battleships", 1)

    # …and a type hangs off the leaf, which is what makes the tree browsable.
    assert ore_db.execute(
        "SELECT name FROM sde_types WHERE market_group_id=517").fetchone()[0] == "Plagioclase"


def test_a_type_with_no_reprocessing_output_simply_has_no_rows(ore_db):
    """Absence is the answer for anything that does not reprocess — not a zero
    row that would read as "refines into nothing"."""
    assert ore_db.execute(
        "SELECT COUNT(*) FROM sde_type_materials WHERE type_id=16262").fetchone()[0] == 0


def test_shipped_ore_yields_match_an_independent_source():
    """Guards the real `sde_base.db`, not a synthetic fixture.

    These per-m³ figures were cross-checked against ore.cerlestes.de and the
    DARK mining spreadsheet, independently of CCP's export, and all three agree.
    If a future SDE import silently changes units — per-unit instead of
    per-batch, assembled instead of packaged volume — this is what notices.
    """
    conn = sqlite3.connect(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sde_base.db"))
    try:
        expected = {
            "Plagioclase": {"Tritanium": 5.0, "Mexallon": 2.0},
            "Spodumain": {"Tritanium": 30.0, "Isogen": 0.625, "Nocxium": 0.1,
                          "Zydrine": 0.05, "Megacyte": 0.025},
            "Veldspar": {"Tritanium": 40.0},
        }
        for ore, wanted in expected.items():
            type_id, volume, portion = conn.execute(
                "SELECT type_id, packaged_volume, portion_size FROM sde_types "
                "WHERE name=?", (ore,)).fetchone()
            got = {name: qty / (volume * portion) for name, qty in conn.execute(
                "SELECT t.name, m.quantity FROM sde_type_materials m "
                "JOIN sde_types t ON t.type_id = m.material_type_id "
                "WHERE m.type_id=?", (type_id,))}
            assert got == pytest.approx(wanted), ore
    finally:
        conn.close()


def test_ice_refines_one_unit_at_a_time_in_the_shipped_data():
    """Appendix A's batch rule, verified against the export rather than quoted:
    100 for ore, 1 for ice."""
    conn = sqlite3.connect(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sde_base.db"))
    try:
        sizes = dict(conn.execute(
            "SELECT name, portion_size FROM sde_types WHERE name IN "
            "('Veldspar', 'Plagioclase', 'Glacial Mass', 'White Glaze')"))
        assert sizes["Veldspar"] == 100
        assert sizes["Plagioclase"] == 100
        assert sizes["Glacial Mass"] == 1
        assert sizes["White Glaze"] == 1
    finally:
        conn.close()
