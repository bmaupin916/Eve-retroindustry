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


@pytest.fixture
def imported(tiny, tmp_path):
    import import_sde
    db = str(tmp_path / "out.db")
    conn = sqlite3.connect(db)
    import_sde.init_db(conn)
    with zipfile.ZipFile(tiny) as z:
        import_sde.import_types(conn, z)
        import_sde.import_groups(conn, z)
        import_sde.import_blueprints(conn, z)
        import_sde.import_planet_schematics(conn, z)
    import_sde.record_build(conn, feed.Build(BUILD, "2026-08-17T11:26:56Z"))
    conn.commit()
    return conn


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


def test_only_manufacturing_and_reaction_are_imported_for_now(imported):
    """Invention is in the source and read by nothing yet. This pins the
    current state so enabling it is a deliberate change, not a surprise."""
    acts = {r[0] for r in imported.execute(
        "SELECT DISTINCT activity FROM sde_blueprint_materials")}
    assert acts == {"manufacturing"}
    assert imported.execute(
        "SELECT COUNT(*) FROM sde_blueprint_products WHERE activity='invention'"
    ).fetchone()[0] == 0


def test_the_build_is_recorded(imported):
    assert imported.execute(
        "SELECT build_number, release_date FROM sde_build WHERE id=1"
    ).fetchone() == (BUILD, "2026-08-17T11:26:56Z")
