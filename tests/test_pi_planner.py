"""PI planner resolver tests — run against the committed ``sde_base.db``.

No network, no ESI, no web app: `PIResolver` takes a db_path so the recipe
graph can be exercised headless.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from app.db.conn import connect_to_path

from app.planetary.planet_data import single_planet_types
from app.web.routers import planets as planets_router
from app.planetary.schematics import PIResolver, split_pi_leaves, whole_units

SDE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sde_base.db")

NITROGEN_FUEL_BLOCK = 4051


@pytest.fixture(scope="module")
def pi():
    resolver = PIResolver(SDE)
    yield resolver
    resolver.close()


def tid(pi: PIResolver, name: str) -> int:
    type_id = pi.find_type_id(name)
    assert type_id is not None, f"{name} missing from sde_types"
    return type_id


# ── the graph ────────────────────────────────────────────────────────────────
def test_tier_derivation(pi):
    """Tier is derived (1 + deepest input), never hardcoded — so it stays
    correct through balance patches that reshuffle the chain."""
    assert pi.tier_of(tid(pi, "Oxygen")) == 1
    assert pi.tier_of(tid(pi, "Coolant")) == 2
    assert pi.tier_of(tid(pi, "Robotics")) == 3
    assert pi.tier_of(tid(pi, "Broadcast Node")) == 4


def test_raw_is_leaf(pi):
    """A type with no row in sde_planet_schematics is a raw P0 and stops the walk."""
    noble_gas = tid(pi, "Noble Gas")
    assert pi.find_schematic(noble_gas) is None
    assert pi.is_pi_commodity(noble_gas) is False
    assert pi.tier_of(noble_gas) == 0

    node = pi.resolve(noble_gas, 3000)
    assert node.is_raw is True
    assert node.tier == 0
    assert node.children == []
    assert node.quantity == 3000


def test_p2_ratio(pi):
    """Ratios come from the SDE: 40 + 40 of two P1 → 5 units per 3600 s cycle."""
    coolant = pi.find_schematic(tid(pi, "Coolant"))
    assert coolant["output_qty"] == 5
    assert coolant["cycle_time"] == 3600
    mats = {m["name"]: m["quantity"] for m in pi.get_materials(coolant["schematic_id"])}
    assert mats == {"Water": 40, "Electrolytes": 40}


def test_robotics_feedstock(pi):
    """Quantities stay fractional — no ceil() per level.

    Robotics is 10 Mechanical Parts + 10 Consumer Electronics per 3 units, so
    one unit costs 10/3 of each. Rounding that up to 4 here would compound
    into a ~20 % overestimate by the time it reaches P0.
    """
    node = pi.resolve(tid(pi, "Robotics"), 1)
    assert node.tier == 3
    feedstock = {c.name: c.quantity for c in node.children}
    assert feedstock == pytest.approx({
        "Mechanical Parts": 10 / 3,
        "Consumer Electronics": 10 / 3,
    })
    # Consumer Electronics appears nowhere in a fuel-block recipe but is real
    # demand the moment Robotics is in the plan.
    assert all(c.tier == 2 for c in node.children)


def test_no_me_applied(pi):
    """Planetary schematics have no material efficiency and no rounding, so
    the whole chain is exactly linear in the target quantity."""
    broadcast_node = tid(pi, "Broadcast Node")
    single = pi.resolve(broadcast_node, 100).leaves()
    double = pi.resolve(broadcast_node, 200).leaves()

    assert set(single) == set(double)
    assert single, "Broadcast Node should bottom out in raw resources"
    for type_id, (_name, qty) in single.items():
        assert double[type_id][1] == pytest.approx(qty * 2)


def test_facility_rates(pi):
    """40 P1/hr, 5 P2/hr, 3 P3/hr, 1 P4/hr — computed from SDE cycle times.

    The load-bearing sanity check for the whole capacity model: if these come
    out at anything else, the cycle-time maths is wrong.
    """
    expected = {1: 40.0, 2: 5.0, 3: 3.0, 4: 1.0}
    rows = pi.conn.execute("SELECT output_type_id FROM sde_planet_schematics").fetchall()
    assert len(rows) > 60, "SDE planet schematics look unpopulated"

    seen = set()
    for row in rows:
        type_id = row["output_type_id"]
        tier = pi.tier_of(type_id)
        seen.add(tier)
        assert tier in expected, f"{pi.get_type_name(type_id)} resolved to tier {tier}"
        assert pi.facility_rate_per_hour(type_id) == pytest.approx(expected[tier])
    assert seen == {1, 2, 3, 4}

    # …and the derived facility count, which is what the colony plan consumes.
    # 216 Coolant/day is one P2 colony at 0.60 derate.
    coolant = tid(pi, "Coolant")
    assert pi.facility_rate_per_day(coolant) == pytest.approx(120.0)
    assert pi.facilities_needed(coolant, 240) == pytest.approx(2.0)


# ── handoff from the manufacturing BOM ───────────────────────────────────────
def test_fuel_block_handoff():
    """The manufacturing BOM bottoms out exactly where the PI graph starts.

    `BOMResolver`'s leaf rule is "no blueprint in the SDE", and PI commodities
    have none — so a fuel-block BOM terminates on the five PI inputs. Ice
    products have no blueprint either, but no schematic either: they are out
    of scope and must land on the non-PI side.
    """
    from app.bom.resolver import BOMResolver

    bom = BOMResolver(connect_to_path(SDE))
    pi = PIResolver(SDE)
    try:
        leaves = bom.resolve(NITROGEN_FUEL_BLOCK, 40, me=10).aggregate_leaves()
        pi_leaves, other = split_pi_leaves(leaves, pi)

        assert {name for name, _qty in pi_leaves.values()} == {
            "Oxygen", "Coolant", "Enriched Uranium", "Mechanical Parts", "Robotics",
        }
        # Ice products, minerals and moon goo: surfaced, not walked.
        assert {"Heavy Water", "Liquid Ozone", "Nitrogen Isotopes"} <= {
            name for name, _qty in other.values()
        }
        assert not (set(pi_leaves) & set(other))

        # The PI half keeps walking, all the way to P0.
        nodes = pi.resolve_many({t: q for t, (_n, q) in pi_leaves.items()})
        by_tier = pi.aggregate_many_by_tier(nodes)
        assert set(by_tier) == {0, 1, 2, 3}      # a fuel block reaches P3, not P4
        assert by_tier[0], "the PI walk must reach raw resources"
    finally:
        bom.close()
        pi.close()


def test_fuel_block_golden():
    """§8's worked example: 50,000 Nitrogen Fuel Blocks/week at blueprint ME 10.

    Fuel blocks are always planned at ME 10. Two things make the numbers come
    out right, and both are the "rate, not job" rule:

    * ME is applied to the *whole period's* runs in one batched job
      (`runs_per_job=None`), so 22 Oxygen/run at ME 10 costs 19.8, not
      ceil(22 × 0.9) = 20. Rounding per 1-run job inflates Oxygen by 1 %.
    * ME reduces the *blueprint's* PI inputs and nothing below them. Robotics
      still costs 10 Mechanical Parts + 10 Consumer Electronics per 3 units.

    Robotics deliberately departs from the figure in §8. EVE floors material
    consumption at one unit per run — `max(runs, ceil(base × runs × 0.9))` —
    so a base quantity of 1 is never reduced by ME at any level. §8 applied
    0.9 to it and got 161/day; the real number is 178.57, which carries
    through to Consumer Electronics and to Robotics' share of Mechanical
    Parts. It does not move any colony count — see `test_fuel_block_colonies`.
    """
    from app.bom.resolver import BOMResolver

    bom = BOMResolver(connect_to_path(SDE), runs_per_job=None)   # one batched job = exact ME
    pi = PIResolver(SDE)
    try:
        # 1,250 runs of 40 blocks = 50,000/week → 178.57 runs/day.
        leaves = bom.resolve(NITROGEN_FUEL_BLOCK, 50_000, me=10).aggregate_leaves()
        pi_leaves, _other = split_pi_leaves(leaves, pi)
        per_day = {name: qty / 7 for name, qty in pi_leaves.values()}

        assert per_day == pytest.approx({
            "Oxygen":           3535.71,   # 22 × 0.9 × 178.57
            "Coolant":          1446.43,   #  9 × 0.9
            "Enriched Uranium":  642.86,   #  4 × 0.9
            "Mechanical Parts":  642.86,   #  4 × 0.9 — direct demand only
            "Robotics":          178.57,   #  1 × 1.0 — ME floors at 1/run
        }, abs=0.01)

        # Robotics is a P3: expanding it adds P2 demand the recipe never lists.
        by_tier = pi.aggregate_many_by_tier(
            pi.resolve_many({t: q / 7 for t, (_n, q) in pi_leaves.items()})
        )
        by_name = {pi.get_type_name(t): q for t, q in by_tier[2].items()}
        assert by_name["Consumer Electronics"] == pytest.approx(595.24, abs=0.01)
        # …and nearly doubles Mechanical Parts over the 643 the recipe lists.
        assert by_name["Mechanical Parts"] == pytest.approx(642.86 + 595.24, abs=0.01)

        # What the page actually shows: whole units, ceiled at the edge.
        assert {n: whole_units(q) for n, q in by_name.items()} == {
            "Coolant": 1447,
            "Mechanical Parts": 1239,
            "Enriched Uranium": 643,
            "Consumer Electronics": 596,
        }
    finally:
        bom.close()
        pi.close()


def test_fuel_block_colonies():
    """§8's colony plan: the same 50,000 blocks/week at derate 0.60.

    The plan lists six products, and *which* six is the whole trick. Total
    demand by tier also contains Water, Electrolytes, Precious Metals and so
    on — but those are refined on-planet inside the P2 extraction colonies
    that consume them, so they get no colony of their own. Robotics is a
    factory product, so its Mechanical Parts and Consumer Electronics *are*
    imported and do.
    """
    from app.bom.resolver import BOMResolver
    from app.planetary.colonies import plan_colonies

    bom = BOMResolver(connect_to_path(SDE), runs_per_job=None)
    pi = PIResolver(SDE)
    try:
        leaves = bom.resolve(NITROGEN_FUEL_BLOCK, 50_000, me=10).aggregate_leaves()
        pi_leaves, _other = split_pi_leaves(leaves, pi)
        roots = pi.resolve_many({t: q / 7 for t, (_n, q) in pi_leaves.items()})

        plan = {r.name: r for r in plan_colonies(pi, roots, derate=0.60)}

        assert {n: r.colonies for n, r in plan.items()} == {
            "Coolant": 7,               # 1,446.43 / 216 per colony
            "Mechanical Parts": 6,      # 1,238.10 / 216
            "Enriched Uranium": 3,      #   642.86 / 216
            "Consumer Electronics": 3,  #   595.24 / 216
            "Oxygen": 1,                # 3,535.71 / 4,032 — P1 colony
            "Robotics": 1,              # factory colony, 3 AIF
        }
        # The internally-refined feedstock must not appear.
        assert "Water" not in plan and "Precious Metals" not in plan

        # 360/day theoretical × 0.60, and 6,720 × 0.60 for the P1 colony.
        assert plan["Coolant"].output_per_colony == pytest.approx(216.0)
        assert plan["Oxygen"].output_per_colony == pytest.approx(4032.0)

        # Robotics: 178.57/day ÷ 72/day per AIF → 3 AIF, which fits one colony.
        robotics = plan["Robotics"]
        assert robotics.kind == "factory"
        assert robotics.facilities == 3
        assert robotics.layout.fits()

        # Every P2 here is single-planet siteable, so none is factory-only.
        assert not any(r.factory_only for r in plan.values())
        assert plan["Enriched Uranium"].planet_types == ["Plasma"]
    finally:
        bom.close()
        pi.close()


def test_archetypes_are_powergrid_bound():
    """§5 claims PG binds every extraction layout — verified, not trusted.

    It is not a universal rule, which is why it is worth computing: the P4
    factory colony inverts it. A High-Tech Production Plant costs 1,100 CPU
    against only 400 PG, so CPU runs out first there.
    """
    from app.planetary.colonies import (
        ADVANCED_INDUSTRY, HIGH_TECH_PLANT, P1_EXTRACTION, P2_EXTRACTION,
        factory_layout, max_facilities,
    )

    for layout in (P2_EXTRACTION, P1_EXTRACTION):
        assert layout.fits(), layout.name
        assert layout.binding_resource() == "PG", layout.name

    # The P2 layout's published numbers, as a guard on the cost tables.
    assert P2_EXTRACTION.cpu() == 8480
    assert P2_EXTRACTION.powergrid() == 17900
    assert P2_EXTRACTION.spare() == (16935, 1100)   # ~1,100 MW left for links

    assert max_facilities(ADVANCED_INDUSTRY) == 14  # PG-bound
    assert max_facilities(HIGH_TECH_PLANT) == 6     # CPU-bound
    assert factory_layout(HIGH_TECH_PLANT, 6).binding_resource() == "CPU"
    assert factory_layout(ADVANCED_INDUSTRY, 14).binding_resource() == "PG"


def test_derate_applies_to_extraction_only():
    """A factory colony runs on imports and is not extraction-limited."""
    from app.planetary.colonies import (
        ADVANCED_INDUSTRY, P2_EXTRACTION, effective_output_per_day,
        factory_layout, theoretical_output_per_day,
    )

    p2_rate = 120.0                      # 5/hr × 24, from the SDE
    assert theoretical_output_per_day(P2_EXTRACTION, 2, p2_rate) == 360.0
    assert effective_output_per_day(P2_EXTRACTION, 2, p2_rate, 0.60) == 216.0
    # Same derate, factory layout → untouched.
    factory = factory_layout(ADVANCED_INDUSTRY, 3)
    assert effective_output_per_day(factory, 3, 72.0, 0.60) == 216.0
    assert theoretical_output_per_day(factory, 3, 72.0) == 216.0


def test_whole_units_rounds_up_only_at_the_edge():
    """Display rounding must not feed back into the walk.

    Ceiling every tier and passing the rounded figure down compounds the
    error four times over a P4 → P0 chain, which is why `resolve()` keeps
    quantities fractional and only the edge is rounded.
    """
    assert whole_units(1446.4285) == 1447
    assert whole_units(0.001) == 1
    assert whole_units(0) == 0
    # Exact values must not drift upwards on float noise.
    assert whole_units(4032.0) == 4032
    assert whole_units(216.00000000001) == 216


# ── planet siting ────────────────────────────────────────────────────────────
def test_two_planet_p2(pi):
    """Three P2s have inputs that share no planet type and need a factory colony."""
    for name in ("Silicate Glass", "Microfiber Shielding", "Polyaramids"):
        assert single_planet_types(name, pi) == [], name
    # Water (6 planet types) ∩ Electrolytes (Gas, Storm).
    assert single_planet_types("Coolant", pi) == ["Gas", "Storm"]


# ── robustness ───────────────────────────────────────────────────────────────
def test_cycle_guard(tmp_path):
    """A cyclic schematic graph must terminate, not blow the stack.

    The real PI graph is acyclic; this guards against ever relying on that.
    """
    db = tmp_path / "cyclic.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sde_types (type_id INTEGER, name TEXT);
        CREATE TABLE sde_planet_schematics (
            schematic_id INTEGER, name TEXT, cycle_time INTEGER,
            output_type_id INTEGER, output_qty INTEGER);
        CREATE TABLE sde_planet_schematic_materials (
            schematic_id INTEGER, type_id INTEGER, quantity INTEGER);
        INSERT INTO sde_types VALUES (1, 'Ouroboros A'), (2, 'Ouroboros B');
        INSERT INTO sde_planet_schematics VALUES
            (10, 'A', 3600, 1, 5),
            (11, 'B', 3600, 2, 5);
        INSERT INTO sde_planet_schematic_materials VALUES (10, 2, 40), (11, 1, 40);
    """)
    conn.commit()
    conn.close()

    pi = PIResolver(str(db))
    try:
        assert pi.tier_of(1) > 0                 # terminates rather than recursing
        node = pi.resolve(1, 5)
        assert node.children[0].name == "Ouroboros B"
        truncated = node.children[0].children[0]
        assert truncated.name == "Ouroboros A"
        assert truncated.children == []          # branch cut here
        assert truncated.is_raw is False         # …and not mistaken for a P0
        assert len(list(node.walk())) == 3
    finally:
        pi.close()


# ── plan vs actual (§7) ──────────────────────────────────────────────────────
#
# Every test here stubs `planets_api.fetch_planets` / `fetch_planet_detail`, the
# only two ESI calls the feature makes. Nothing touches the network.

COOLANT_SCHEMATIC = 66          # → Coolant (P2)
WATER_SCHEMATIC = 121           # → Water (P1), the feedstock a Coolant colony refines
BIOCELLS_SCHEMATIC = 79         # → Biocells (P2), nothing to do with fuel blocks
AQUEOUS_LIQUIDS = 2268          # a P0 an extractor pulls

# conftest seeds two characters. The colonies belong to this one, so counts
# stay legible — serving the same planets to both would double every total.
PI_CHAR = 900000001

FUEL_BLOCK_PLAN = (
    "/pi-planner?target=Nitrogen+Fuel+Block&qty=50000&period=week&derate=60&ccu=5&me=10"
)


def _plan_vs_actual(client, url):
    """Renders the page and returns the `actual` block of the view model.

    Goes through the real route, so `_fetch_pi_colonies` — the shared fetch
    /planets also uses — is genuinely exercised; only the two ESI functions
    underneath it are stubbed.
    """
    captured = {}
    original = planets_router._tr

    def spy(name, request, context):
        captured["view"] = context
        return original(name, request, context)

    # The router imported _tr into its own namespace, so patching main's copy
    # no longer reaches the handler. That is the whole point of the W6 split;
    # the spy has to follow the code.
    planets_router._tr = spy
    try:
        response = client.get(url)
        assert response.status_code == 200, response.text[:400]
    finally:
        planets_router._tr = original
    return captured["view"]["actual"]


def _factory_pin(schematic_id, *, shape="both"):
    """A factory pin.

    Real ESI carries the schematic under `factory_details` *and* repeats it at
    the top level, so "both" is the realistic default. The single-shape
    variants exist to prove the planner reads either one: /planets happens to
    read only the top-level copy, the spec names the nested one.
    """
    pin = {"pin_id": 1}
    if shape in ("both", "nested"):
        pin["factory_details"] = {"schematic_id": schematic_id}
    if shape in ("both", "top"):
        pin["schematic_id"] = schematic_id
    return pin


def _extractor_pin(product_type_id, expiry_iso):
    return {
        "pin_id": 2,
        "expiry_time": expiry_iso,
        "extractor_details": {"product_type_id": product_type_id, "heads": [{}, {}]},
    }


def _in(hours):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def stub_pi(app_module, monkeypatch):
    """Installs a fake colony layout for every signed-in test character.

    Takes {planet_id: [pins]} and writes it into `pi_colony_cache`, which is
    what the pages read.

    It used to stub `fetch_planets` and `fetch_planet_detail`. Both pages
    stopped calling them in v0.9.53 — the sync worker fetches, the pages read —
    so the stubs were installed and ignored and every assertion here failed
    against an empty page. Seeding the cache is the repair *and* the more
    honest fixture: it exercises the path a real request takes rather than one
    that now exists only under test.
    """
    from app.character import planets as planets_api

    # Start each test from an empty extractor cache — the planner writes to it.
    conn = app_module.get_conn()
    try:
        planets_router._ensure_pi_cache_tables(conn)
        conn.execute("DELETE FROM pi_extractor_cache")
        conn.execute("DELETE FROM pi_colony_cache")
        conn.commit()
    finally:
        conn.close()

    def install(colonies_by_planet, forbidden=False):
        from app.db.conn import connect as _connect

        conn = app_module.get_conn()
        try:
            # Planet names, as a real install has them: the worker resolves
            # them once, permanently, during the colony fetch.
            for planet_id in colonies_by_planet:
                conn.execute(
                    "INSERT OR REPLACE INTO planet_name_cache (planet_id, name) VALUES (?,?)",
                    (planet_id, f"Testworld {planet_id}"))
            conn.commit()
        finally:
            conn.close()

        # `token_store` and `app/character/planets.py` are both on the portable
        # query layer now, so everything below asks through the engine. The raw
        # handle above stays only for `INSERT OR REPLACE`, which is SQLite's
        # own spelling and has no portable equivalent worth introducing here.
        with _connect() as ec:
            if forbidden:
                planets_api.save_cached_colonies(
                    ec, PI_CHAR, [], [], planets_api.FORBIDDEN)
            else:
                colonies = [{"planet_id": pid, "planet_type": "barren",
                             "upgrade_level": 5, "num_pins": len(pins)}
                            for pid, pins in colonies_by_planet.items()]
                details = [{"pins": pins, "links": [], "routes": []}
                           for _pid, pins in colonies_by_planet.items()]
                planets_api.save_cached_colonies(ec, PI_CHAR, colonies, details)

            # The other seeded character has no PI — synced, and empty, which is
            # a different thing from never synced and has to be said so.
            for cid, _name in app_module.list_characters(ec):
                if cid != PI_CHAR:
                    planets_api.save_cached_colonies(ec, cid, [], [])
            ec.commit()
    return install


def test_plan_vs_actual_counts_colonies_by_output_product(client, stub_pi):
    """Two colonies running the Coolant schematic → have 2 against a need of 7.

    The count comes from factory pins, not from anything the user typed: the
    schematic joins to sde_planet_schematics for its output type.
    """
    stub_pi({
        4001: [_factory_pin(COOLANT_SCHEMATIC), _factory_pin(COOLANT_SCHEMATIC)],
        4002: [_factory_pin(COOLANT_SCHEMATIC)],
    })
    r = client.get(FUEL_BLOCK_PLAN)
    assert r.status_code == 200

    view = _plan_vs_actual(client, FUEL_BLOCK_PLAN)
    coolant = next(row for row in view["rows"] if row["name"] == "Coolant")
    assert coolant["needed"] == 7
    assert coolant["have"] == 2          # two planets, three factory pins
    assert coolant["facilities"] == 3
    assert coolant["gap"] == 5
    # Untouched products are all gap.
    oxygen = next(row for row in view["rows"] if row["name"] == "Oxygen")
    assert (oxygen["needed"], oxygen["have"], oxygen["gap"]) == (1, 0, 1)


@pytest.mark.parametrize("shape", ["nested", "top", "both"])
def test_plan_vs_actual_reads_either_schematic_shape(client, stub_pi, shape):
    """`factory_details.schematic_id` is the documented location and the one
    the spec names; ESI also repeats it at the top level. A payload carrying
    only one of them must still count."""
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC, shape=shape)]})
    view = _plan_vs_actual(client, FUEL_BLOCK_PLAN)
    coolant = next(row for row in view["rows"] if row["name"] == "Coolant")
    assert coolant["have"] == 1


def test_plan_vs_actual_forbidden_prompts_reauth(client, stub_pi):
    """A token predating the PI scope prompts a re-auth, exactly as /planets
    does — it must not surface as an error or a 500."""
    stub_pi({}, forbidden=True)
    r = client.get(FUEL_BLOCK_PLAN)
    assert r.status_code == 200
    assert "PI access not authorized" in r.text
    assert "/auth/login" in r.text

    view = _plan_vs_actual(client, FUEL_BLOCK_PLAN)
    assert view["needs_relogin"]              # named characters, not a crash
    assert view["fetched_any"] is False
    assert all(row["have"] == 0 for row in view["rows"])


def test_expiring_extractors_only_on_colonies_the_plan_needs(client, stub_pi):
    """The 24h warning is scoped to colonies the plan actually depends on.

    Both colonies below have an extractor expiring in 3h. Only the one running
    a Coolant factory is in the fuel-block plan, so only it is surfaced —
    otherwise every unrelated colony would raise a false alarm.
    """
    stub_pi({
        4001: [_factory_pin(COOLANT_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(3))],
        4002: [_factory_pin(BIOCELLS_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(3))],
    })
    view = _plan_vs_actual(client, FUEL_BLOCK_PLAN)

    assert len(view["expiring"]) == 1
    alert = view["expiring"][0]
    assert alert["planet_name"] == "Testworld 4001"   # from the shared name cache
    assert alert["product"] == "Aqueous Liquids"
    assert alert["for_products"] == ["Coolant"]
    assert alert["expired"] is False
    # Biocells is real production, just not part of this plan.
    assert "Biocells" in view["unplanned"]


def test_expiring_window_is_24h(client, stub_pi):
    """A program with more than a day left is not an alert."""
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(48))]})
    assert _plan_vs_actual(client, FUEL_BLOCK_PLAN)["expiring"] == []

    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(-2))]})
    expiring = _plan_vs_actual(client, FUEL_BLOCK_PLAN)["expiring"]
    assert len(expiring) == 1 and expiring[0]["expired"] is True


def test_feedstock_colony_counts_toward_its_own_p1(client, stub_pi):
    """A P2 colony's BIF really do make P1, so they count where the plan asks
    for that P1 — here a Coolant colony's Water facility covers nothing in the
    fuel-block plan, but is still reported as production."""
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC), _factory_pin(WATER_SCHEMATIC)]})
    view = _plan_vs_actual(client, FUEL_BLOCK_PLAN)
    assert next(r for r in view["rows"] if r["name"] == "Coolant")["have"] == 1
    assert "Water" in view["unplanned"]      # not a planned product — it's internal


def test_plan_vs_actual_absent_without_a_plan(client, stub_pi):
    """No target → no ESI work and no section 4."""
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC)]})
    r = client.get("/pi-planner")
    assert r.status_code == 200
    assert "Plan vs actual" not in r.text


def test_planets_page_still_renders_through_the_shared_fetch(client, stub_pi):
    """`_fetch_pi_colonies` was extracted out of /planets so /pi-planner could
    reuse it. /planets has no smoke coverage (it needs ESI), so guard the
    refactor here: same stub, both pages, one fetch path.
    """
    stub_pi({
        4001: [_factory_pin(COOLANT_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(3))],
    })
    r = client.get("/planets")
    assert r.status_code == 200
    assert "Coolant" in r.text                 # production chain rendered
    assert "Aqueous Liquids" in r.text         # extractor rendered


def test_planets_page_forbidden_still_prompts_reauth(client, stub_pi):
    """The "forbidden" contract survives the extraction on /planets too."""
    stub_pi({}, forbidden=True)
    r = client.get("/planets")
    assert r.status_code == 200
    assert "PI access not authorized" in r.text


# ── shared PI extractor cache ────────────────────────────────────────────────
def _cached_extractors(app_module):
    conn = app_module.get_conn()
    try:
        return conn.execute(
            "SELECT planet_id, planet_name, product, char_id FROM pi_extractor_cache "
            "ORDER BY planet_id"
        ).fetchall()
    finally:
        conn.close()


def test_planner_refreshes_the_shared_extractor_cache(client, stub_pi, app_module):
    """The planner writes to the same store /planets does — no second cache.

    The payload covers *every* extractor, not just the plan-relevant ones the
    page displays: `_store_pi_cache_for_chars` replaces a character's rows
    wholesale, so handing it the filtered list would quietly drop everything
    else from the dashboard tile and the nav badge.
    """
    stub_pi({
        4001: [_factory_pin(COOLANT_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(3))],
        4003: [_factory_pin(BIOCELLS_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(2))],
    })
    view = _plan_vs_actual(client, FUEL_BLOCK_PLAN)
    assert len(view["expiring"]) == 1              # the page shows only the planned one

    rows = _cached_extractors(app_module)
    assert [r[0] for r in rows] == [4001, 4003]    # …the cache keeps both
    assert rows[0][1] == "Testworld 4001"          # real name, not a "Planet #id" placeholder
    assert rows[0][2] == "Aqueous Liquids"
    assert all(r[3] == PI_CHAR for r in rows)


def test_cached_extractors_feed_the_nav_badge(client, stub_pi, app_module):
    """What the planner wrote is what /api/pi-alert-count counts."""
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(3))]})
    _plan_vs_actual(client, FUEL_BLOCK_PLAN)

    summary = client.get("/api/pi-alert-count").json()
    assert summary["n_alert"] == 1
    assert summary["n_expired"] == 0


def test_forbidden_fetch_leaves_the_cache_alone(client, stub_pi, app_module):
    """A character whose fetch failed keeps its last-known rows.

    The store is per-character and skips an empty id list, so a page load that
    got nothing back must not blank a cache a previous load filled.
    """
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC), _extractor_pin(AQUEOUS_LIQUIDS, _in(3))]})
    _plan_vs_actual(client, FUEL_BLOCK_PLAN)
    before = _cached_extractors(app_module)
    assert before

    stub_pi({}, forbidden=True)
    _plan_vs_actual(client, FUEL_BLOCK_PLAN)
    assert _cached_extractors(app_module) == before


def test_planner_does_not_fetch_planet_names_it_already_has(client, stub_pi, app_module, monkeypatch):
    """Planet names come from the shared cache; a cached planet costs no ESI."""
    calls = []
    real = planets_router._resolve_planet_names

    # Synchronous now: the resolver reads the cache and never fetches, so there
    # is nothing to await. The sync worker resolves a planet's name once, ever,
    # during the colony fetch.
    def spy(conn, planet_ids):
        calls.append(set(planet_ids))
        return real(conn, planet_ids)

    monkeypatch.setattr(planets_router, "_resolve_planet_names", spy)
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC)]})
    view = _plan_vs_actual(client, FUEL_BLOCK_PLAN)

    assert calls == [{4001}]
    # Cached by the fixture, so the resolver returned it without a miss.
    assert next(r for r in view["rows"] if r["name"] == "Coolant")["sources"] == [
        "Test Pilot Alpha — Testworld 4001"
    ]


# ── the SQL underneath /planets and the alert cache ──────────────────────────
#
# Added before the query conversion, because mutating the eighteen statements in
# `routers/planets.py` and `pi_planner_helper.py` against the net above caught
# **6 of 15**. What follows closes the gaps that were real.
#
# Two survivors were *not* gaps, and are recorded here rather than given a test
# that would pass for the wrong reason:
#
# * **The schematic lookup's `IN` list is a size filter.** Dropping it returns
#   every schematic in the SDE, and the extras are never rendered — the page
#   only reads `schematics[sid]` for ids its own pins name. A superset cannot
#   change the output, so no assertion can see it.
# * **`AND sell_price IS NOT NULL` on the price map is redundant.** Both readers
#   guard with `if out_price` / `if p0`, and a `None` value is exactly as falsy
#   as a missing key.
#
# Same finding as the assets router in v0.9.68, and worth stating twice: an `IN`
# list that only ever narrows a lookup the caller then indexes by key is a
# performance filter, not a correctness one.

def _planets_view(client) -> dict:
    """The `/planets` template context, captured through the real route."""
    captured = {}
    original = planets_router._tr

    def spy(name, request, context):
        captured["view"] = context
        return original(name, request, context)

    planets_router._tr = spy
    try:
        assert client.get("/planets").status_code == 200
    finally:
        planets_router._tr = original
    return captured["view"]


def _only_colony(view: dict) -> dict:
    """The single colony in the view.

    Both seeded characters appear in `groups` — the second one synced and empty,
    which the page deliberately distinguishes from never synced — so this picks
    the group that actually has colonies rather than assuming there is one.
    """
    with_colonies = [g for g in view["groups"] if g["colonies"]]
    assert len(with_colonies) == 1, (
        f"expected exactly one character with colonies: "
        f"{[(g['char_id'], len(g['colonies'])) for g in view['groups']]}")
    colonies = with_colonies[0]["colonies"]
    assert len(colonies) == 1, f"expected one colony: {colonies}"
    return colonies[0]


def test_a_production_chain_lists_the_inputs_its_schematic_consumes(client, stub_pi):
    """`sde_planet_schematic_materials`, which nothing reached before.

    A Coolant factory consumes Water and Electrolytes. Without the materials
    query the chain still renders — output, cycle time and count all come from
    the *other* schematic table — so it degrades into a production chain saying
    the colony makes Coolant out of nothing.
    """
    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC)]})

    colony = _only_colony(_planets_view(client))

    chain = next(p for p in colony["production"] if p["output"] == "Coolant")
    inputs = {i["name"] for i in chain["inputs"]}
    assert inputs, "the production chain lists no inputs at all"
    assert "Water" in inputs, f"Coolant is refined from Water: {inputs}"


def test_a_colony_is_valued_from_the_cached_sell_prices(client, stub_pi, app_module):
    """`market_price_cache` feeds the est. output-value/day hint.

    The figure is `price × output_qty × count × cycles_per_day`, so a lookup
    that came back empty shows a colony worth **0** rather than raising — the
    shape of failure this section exists to catch.
    """
    import time as _t

    stub_pi({4001: [_factory_pin(COOLANT_SCHEMATIC)]})

    conn = app_module.get_conn()
    try:
        coolant = conn.execute(
            "SELECT output_type_id FROM sde_planet_schematics WHERE schematic_id=?",
            (COOLANT_SCHEMATIC,)).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO market_price_cache"
            " (type_id, sell_price, buy_price, cached_at) VALUES (?,?,?,?)",
            (coolant, 1200.0, 1000.0, _t.time()))
        conn.commit()
    finally:
        conn.close()

    colony = _only_colony(_planets_view(client))

    assert colony["value_day"] > 0, (
        "a colony producing a priced commodity was valued at zero — the price "
        "lookup returned nothing")


# ── pi_extractor_cache: the per-character replace ────────────────────────────

def _cached_rows(app_module) -> list[tuple]:
    conn = app_module.get_conn()
    try:
        return conn.execute(
            "SELECT char_id, planet_id, product_id, expiry_iso"
            " FROM pi_extractor_cache ORDER BY planet_id, product_id").fetchall()
    finally:
        conn.close()


def test_an_extractor_that_stopped_is_removed_from_the_cache(client, stub_pi, app_module):
    """The `DELETE ... WHERE char_id IN (…)` that runs before the insert.

    The write is a *replace* per character, not an upsert: an extractor the
    character has since torn down has no new row to overwrite it, so without the
    delete it stays cached forever and the dashboard keeps counting an expiry
    that will never arrive.

    Two planets down to one is the smallest fixture that can show it — with a
    single planet, "replaced" and "upserted" are the same observation.
    """
    stub_pi({
        4001: [_extractor_pin(AQUEOUS_LIQUIDS, _in(5))],
        4002: [_extractor_pin(AQUEOUS_LIQUIDS, _in(6))],
    })
    _planets_view(client)
    assert {r[1] for r in _cached_rows(app_module)} == {4001, 4002}

    stub_pi({4001: [_extractor_pin(AQUEOUS_LIQUIDS, _in(5))]})
    _planets_view(client)

    assert {r[1] for r in _cached_rows(app_module)} == {4001}, (
        "a planet the character no longer extracts from is still cached")


def test_two_extractors_on_one_planet_collapse_to_a_single_row(client, stub_pi, app_module):
    """`ON CONFLICT (char_id, planet_id, product_id) DO UPDATE`, reached the only
    way it *can* be reached.

    **The first version of this test was wrong about why it passed.** It stored
    one extractor, then stored it again with a later expiry, and claimed the
    upsert was what moved the time. It is not: `_store_pi_cache_for_chars`
    issues `DELETE ... WHERE char_id IN (…)` before the insert, and every entry's
    character is in that list — so across two calls there is nothing left to
    conflict with, and the mutation battery said so by not catching a mutation
    that gutted the `DO UPDATE` clause.

    The clause is reachable **within a single batch**: two extractor pins on the
    same planet pulling the same P0 produce two rows with the same key, and the
    second conflicts with the first. That is an ordinary colony layout, not a
    corner case.

    Note what this pins, because it is a decision rather than an inevitability:
    the surviving row carries the **last** pin's expiry, not the soonest. The
    dashboard therefore counts down to one of the two heads. Worth revisiting —
    an alert probably wants the earlier one — but it is pre-existing behaviour
    and not something a query conversion should quietly change.
    """
    early, late = _in(2), _in(40)
    stub_pi({4001: [
        dict(_extractor_pin(AQUEOUS_LIQUIDS, early), pin_id=21),
        dict(_extractor_pin(AQUEOUS_LIQUIDS, late), pin_id=22),
    ]})

    _planets_view(client)
    rows = _cached_rows(app_module)

    assert len(rows) == 1, (
        f"two extractors on one planet should share one cache row: {rows}")
    assert rows[0][3] == late, (
        "the conflicting insert did not update the expiry — the DO UPDATE "
        "clause is not doing anything")


# ── the alert summary the dashboard tile reads ───────────────────────────────

def test_the_alert_names_the_product_and_the_planet(client, stub_pi):
    """`sde_types` and `planet_name_cache` inside `_pi_refresh_alerts`, each
    looked up by an `IN` list.

    Neither is load-bearing for the alert *count*, which is why the existing net
    missed both: the fallbacks are `#2268` and `Planet #4001`, so a lookup that
    returns nothing still produces a perfectly well-formed alert — one naming an
    item by its type id and a world by its number.

    **`force=1` is doing real work here.** Rendering `/planets` writes the
    extractor cache itself, with names resolved by the *page's* lookups — so a
    plain call to this endpoint finds the cache fresh, skips
    `_pi_refresh_alerts` entirely, and asserts on rows the rebuild never
    touched. The first version of this test did exactly that and could not see
    either lookup break. Forcing the refresh is what puts the two statements
    under test on the path.
    """
    stub_pi({4001: [_extractor_pin(AQUEOUS_LIQUIDS, _in(3))]})
    _planets_view(client)

    body = client.get("/api/dashboard/pi-alerts?force=1").json()

    assert body["items"], f"no alert for an extractor due in 3h: {body}"
    item = body["items"][0]
    assert item["product"] == "Aqueous Liquids", (
        f"the product fell back to its type id: {item['product']}")
    assert item["planet"] == "Testworld 4001", (
        f"the planet fell back to its id: {item['planet']}")


# ── pi_planner_helper: the two SDE lookups behind the target box ─────────────

def test_the_target_box_is_offered_things_pi_can_actually_make(client):
    """`target_choices` — a UNION of every PI schematic output and every
    blueprint product with a PI commodity in its bill of materials.

    It had **no assertion at all**. The pre-conversion battery appeared to catch
    a mutation here, but only because that mutation renamed the function and
    every test died on the `NameError` — a crash is not coverage. Blanking the
    return value instead was caught by nothing.

    Two members, chosen from opposite arms of the UNION: Water is a schematic
    output, and a fuel block is a manufactured item that consumes PI. A query
    that lost either arm still returns a long plausible list.
    """
    from app.db.location import database_path

    from app.web import pi_planner_helper as helper

    view = helper.build_view_model(database_path())
    choices = view["target_choices"]

    assert choices, "the target box was offered nothing at all"
    assert "Water" in choices, "a PI schematic output is missing from the list"
    assert "Nitrogen Fuel Block" in choices, (
        "a manufactured item that consumes PI is missing — the second arm of "
        "the UNION returned nothing")


def test_a_partial_target_resolves_to_the_shortest_match(client):
    """`ORDER BY LENGTH(name) LIMIT 1` on the prefix fallback.

    **The search term here is load-bearing and the first one I picked was not.**
    "hobgoblin" matches five types and the shortest, *Hobgoblin I*, is also the
    one SQLite happens to return first — so dropping the `ORDER BY` changed
    nothing and the mutation survived. A test whose expected value coincides
    with the unordered answer proves only that a row came back.

    "nitrogen" separates them. Three types match, and without the ordering
    SQLite yields **Nitrogen Fuel Block** (19 characters) before **Nitrogen
    Isotopes** (17) — so the assertion below fails the moment the `ORDER BY`
    goes. It is also the realistic complaint: typing a partial name and being
    handed the longer, less likely item.

    The two backends are under no obligation to agree on unordered row order,
    which is exactly the class of difference a conversion must not introduce.

    Note the exact-name lookup above it is *not* pinned, deliberately: if a type
    is named exactly what you typed then it is also the shortest prefix match,
    so the fast path and the fallback cannot disagree. Removing it changes
    nothing observable, and a test claiming otherwise would describe something
    that is not true.
    """
    from app.db.conn import connect

    from app.web import pi_planner_helper as helper

    with connect() as c:
        row = helper._find_target(c, "nitrogen")

    assert row is not None, "a prefix that matches three types resolved to nothing"
    assert row[1] == "Nitrogen Isotopes", (
        f"expected the shortest match, got {row[1]!r} — the ORDER BY LENGTH is "
        f"not being applied")


def test_a_target_typed_as_an_id_resolves_to_that_type(client):
    """The `isdigit()` branch, which nothing else reaches."""
    from app.db.conn import connect

    from app.web import pi_planner_helper as helper

    with connect() as c:
        row = helper._find_target(c, "2454")

    assert row is not None and row[0] == 2454
    assert row[1] == "Hobgoblin I"


def test_a_fresh_alert_cache_is_served_without_rebuilding(client, stub_pi):
    """`SELECT MAX(cached_at)` is what makes the dashboard tile cache-first.

    `age` decides `fresh`, and `fresh` decides whether the summary is rebuilt
    from the colony cache on every poll. An age that always came back `None`
    reads as "nothing cached yet", so the rebuild would run every time — no
    error and no wrong number, just work on a timer nobody asked for.
    """
    stub_pi({4001: [_extractor_pin(AQUEOUS_LIQUIDS, _in(3))]})
    _planets_view(client)

    body = client.get("/api/dashboard/pi-alerts").json()

    assert body.get("from_cache") is True, (
        "the tile rebuilt the alert cache although it had just been written — "
        "the cache age is not being read")
    assert body["age"] is not None
