"""The five SQL statements in `app/web/routers/assets.py` that nothing covered.

Written **before** the conversion to the portable query layer, against the
`sqlite3` version, so the rewrite is judged by whether it preserves them. Same
order of work that found the `_ROUTE_CHUNK` bug in v0.9.67: the net goes in
first, and it is allowed to fail the code it is describing.

`load_route_jumps`/`save_route_jumps` have their own file, and the character
container resolver is covered by `test_assets_tree.py`. What was left with no
behavioural assertion at all:

===========================  ===================================================
statement                    what it decides
===========================  ===================================================
`sde_blueprints`             whether a blueprint is badged RXN or BPO
`location_name_cache` (page) the `data-sys-id` each station carries
`location_name_cache` (api)  which locations `/api/assets/distances` answers for
`sde_types` (corp)           whether a corp container shows its name or a number
`sde_blueprint_products`     where a blueprint's "Plan" button points
===========================  ===================================================

**Every one of these fails silently.** None of them raise on a wrong answer —
a badge turns into the wrong badge, a station loses an attribute, a "Plan"
button points at the blueprint instead of the product. That is the argument for
covering them before touching them, rather than trusting the page to keep
rendering.

Three fixture notes, all learned the hard way in this repo:

* **The session DB is shared.** These fixtures write to it and put it back,
  exactly like `assembled_ship` in `test_assets_tree.py`. Anything left behind
  shows up as an unrelated failure in another file.
* **Two of everything being filtered.** One station, one blueprint or one
  activity makes "returned the right one" and "returned all of them" the same
  observation.
* **Real SDE ids, not invented ones.** These queries read `sde_*`, which the
  suite builds from the committed `sde_base.db`. Inventing a blueprint id would
  test the fixture rather than the query.

**Two of these statements turned out not to be load-bearing at all**, which the
mutation battery said and a rendering probe then confirmed by producing
byte-identical HTML with each one broken:

* The reaction query's `blueprint_type_id IN (…)` is a **size** filter, not a
  correctness one. `reaction_bp_types` is only ever asked about type ids that
  came from `all_bp_type_ids` in the first place, so returning every reaction
  blueprint in the SDE instead of the handful requested cannot change a badge.
  It is still worth keeping — it is 120 rows against a few — but no test can
  justify it, and pretending otherwise would mean writing one that passes for
  the wrong reason.
* The page's `WHERE solar_system_id IS NOT NULL` is redundant, for the reason
  written out where that test used to be.
* The container resolvers' `sde_types … WHERE type_id IN (…)` is the same
  shape: the result is only ever read as `type_names.get(asset["type_id"])`, so
  returning every type in the SDE gives the same names. Here the `IN` earns its
  place on size alone — `sde_types` has tens of thousands of rows — but that is
  a performance argument and this file does not pretend to make it. A mutation
  that replaces the `IN` with a `LIMIT` **is** caught, because that drops rows
  the callers need; the superset is what nothing can see.

Both are recorded rather than papered over. A mutation that survives is either a
hole in the net or a fact about the code, and the two are only told apart by
measuring.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.web.routers import assets as assets_router

# ── real ids out of the bundled SDE ──────────────────────────────────────────
# Chosen because each one distinguishes a filter that could otherwise be
# dropped without any test noticing.

#: reaction_time = 10800, so `reaction_time > 0` is true. Badges RXN.
RXN_BP = 46157              # Methanofullerene Reaction Formula
RXN_PRODUCT = 30306         # its only product, activity 'reaction'

#: reaction_time = 0. Badges BPO, and carries **two** product rows —
#: `manufacturing` → 582 and `invention` → 39581 — which is what makes it the
#: right blueprint for pinning `activity IN ('manufacturing','reaction')`.
MANU_BP = 683               # Bantam Blueprint
MANU_PRODUCT = 582          # Bantam,           activity 'manufacturing'
INVENTION_PRODUCT = 39581   # never correct here, activity 'invention'

#: Twelve product rows, **all** of them `invention`, and no manufacturing or
#: reaction row at all. That is what makes it the blueprint that pins the
#: activity filter deterministically: with the filter it is simply absent from
#: `product_type_map` and the Plan button falls back to its own type id, and
#: without the filter it maps to one of the twelve. Which one does not matter,
#: which is the whole point — see the test at the bottom of this file.
INVENTION_ONLY_BP = 30187   # Intact Thruster Sections

MEGATHRON = 641
BADGER = 648

CHAR = 900000002            # the non-owner seeded character
CORP = 98000001

JITA = 60003760             # Jita IV-4, in the SDE
JITA_SYSTEM = 30000142
AMARR_SYSTEM = 30002187


# ── helpers ──────────────────────────────────────────────────────────────────

def _bp(type_id: int, item_id: int, *, is_original: bool = True) -> dict:
    """One entry shaped like the ESI blueprints endpoint returns them."""
    return {
        "item_id": item_id, "type_id": type_id, "location_id": JITA,
        # quantity == -1 is what marks a BPO; anything else reads as a copy and
        # `_bp_kind` short-circuits to "bpc" before it ever consults sde_blueprints.
        "location_flag": "Hangar", "quantity": -1 if is_original else -2,
        "material_efficiency": 10, "time_efficiency": 20, "runs": -1,
    }


def _asset(type_id: int, item_id: int, *, location_id: int = JITA) -> dict:
    return {
        "item_id": item_id, "type_id": type_id, "quantity": 1,
        "location_id": location_id, "location_flag": "Hangar",
        "is_singleton": False,
    }


class _Restore:
    """Swap a character's cached blob out and put the original back.

    The suite's DB is session-scoped and every other assets test reads from it,
    so a fixture that leaves its own blueprints behind changes what those tests
    see. `char_blueprints_cache` has no unique constraint on `character_id`
    either, so this deletes before inserting rather than relying on REPLACE —
    the same trap `assembled_ship` documents for `char_assets_cache`.
    """

    def __init__(self, app_module, table: str, character_id: int):
        self.m, self.table, self.cid = app_module, table, character_id
        self.original: tuple | None = None

    def install(self, payload: list) -> None:
        conn = self.m.get_conn()
        row = conn.execute(
            f"SELECT data_json, cached_at FROM {self.table} WHERE character_id=?",
            (self.cid,)).fetchone()
        self.original = (row[0], row[1]) if row else None
        conn.execute(f"DELETE FROM {self.table} WHERE character_id=?", (self.cid,))
        conn.execute(
            f"INSERT INTO {self.table} (character_id, data_json, cached_at)"
            " VALUES (?,?,?)", (self.cid, json.dumps(payload), time.time()))
        conn.commit()
        conn.close()

    def restore(self) -> None:
        conn = self.m.get_conn()
        conn.execute(f"DELETE FROM {self.table} WHERE character_id=?", (self.cid,))
        if self.original:
            conn.execute(
                f"INSERT INTO {self.table} (character_id, data_json, cached_at)"
                " VALUES (?,?,?)", (self.cid, self.original[0], self.original[1]))
        conn.commit()
        conn.close()


@pytest.fixture
def two_blueprints(app_module):
    """One reaction formula and one manufacturing blueprint, both originals.

    Two, and of different kinds, because a single blueprint cannot tell
    "badged by its reaction_time" from "badged the same way every time".
    """
    bps = _Restore(app_module, "char_blueprints_cache", CHAR)
    assets = _Restore(app_module, "char_assets_cache", CHAR)
    bps.install([_bp(RXN_BP, 700001), _bp(MANU_BP, 700002),
                 _bp(INVENTION_ONLY_BP, 700003)])
    assets.install([_asset(RXN_BP, 700001), _asset(MANU_BP, 700002),
                    _asset(INVENTION_ONLY_BP, 700003)])
    yield
    bps.restore()
    assets.restore()


@pytest.fixture
def two_systems(app_module):
    """Three cached locations: two in different systems, one with no system.

    The third is the point. `WHERE solar_system_id IS NOT NULL` is invisible
    with only resolvable stations in the table, and a structure whose system the
    resolver never learned is the ordinary case that puts a NULL there.

    **It also clears `route_jump_cache` for these systems, at both ends.** The
    first version did not, and `test_the_second_call_is_served_from_the_cache`
    failed on its *first* call because an earlier test in this same file had
    already warmed the shared cache. The test was correct and the fixture was
    not: it made the result depend on execution order, and it left rows behind
    for every other file to trip over. A cache test has to start cold by
    construction, not by being run first.
    """
    rows = [
        (JITA, "Jita IV - Moon 4", JITA_SYSTEM),
        (60008494, "Amarr VIII - Emperor Family Academy", AMARR_SYSTEM),
        (1030000000001, "Some Unresolved Structure", None),
    ]
    systems = (JITA_SYSTEM, AMARR_SYSTEM)

    def _clear_routes(conn) -> None:
        conn.execute(
            "DELETE FROM route_jump_cache WHERE sys_a IN (?,?) OR sys_b IN (?,?)",
            systems + systems)

    conn = app_module.get_conn()
    saved = conn.execute(
        "SELECT location_id, name, solar_system_id FROM location_name_cache"
        " WHERE location_id IN (?,?,?)", tuple(r[0] for r in rows)).fetchall()
    for loc_id, name, sys_id in rows:
        conn.execute(
            "INSERT OR REPLACE INTO location_name_cache"
            " (location_id, name, solar_system_id) VALUES (?,?,?)",
            (loc_id, name, sys_id))
    _clear_routes(conn)
    conn.commit()
    conn.close()

    yield rows

    conn = app_module.get_conn()
    conn.execute("DELETE FROM location_name_cache WHERE location_id IN (?,?,?)",
                 tuple(r[0] for r in rows))
    for loc_id, name, sys_id in saved:
        conn.execute(
            "INSERT OR REPLACE INTO location_name_cache"
            " (location_id, name, solar_system_id) VALUES (?,?,?)",
            (loc_id, name, sys_id))
    _clear_routes(conn)
    conn.commit()
    conn.close()


# ── sde_blueprints: the RXN badge ────────────────────────────────────────────

def test_a_reaction_formula_is_badged_rxn_and_a_blueprint_bpo(client, two_blueprints):
    """`reaction_time > 0` is the whole difference between the two badges.

    Both blueprints are originals, so `_bp_kind` reaches the reaction lookup for
    both and the *only* thing separating them is what `sde_blueprints` says. A
    query that returned everything would badge the Bantam RXN; one that returned
    nothing would badge the formula BPO. Asserting both directions is what
    makes those two mutations distinguishable.
    """
    html = client.get(f"/assets?view={CHAR}").text

    assert "Methanofullerene Reaction Formula" in html, (
        "the fixture's blueprints never reached the page — the assertions below "
        "would pass vacuously")
    rxn_row = _row_containing(html, "Methanofullerene Reaction Formula")
    bpo_row = _row_containing(html, "Bantam Blueprint")

    assert ">RXN<" in rxn_row, f"reaction formula was not badged RXN: {rxn_row!r}"
    assert ">BPO<" in bpo_row, f"manufacturing blueprint was not badged BPO: {bpo_row!r}"
    assert ">RXN<" not in bpo_row, (
        "a blueprint with reaction_time = 0 was badged as a reaction formula — "
        "the `reaction_time > 0` filter is not doing anything")


def _row_containing(html: str, needle: str) -> str:
    """The <tr>…</tr> around `needle`.

    The badge assertions have to be per-row: the page renders both blueprints,
    so a bare `">RXN<" in html` passes no matter which row carries it.
    """
    assert needle in html, f"{needle!r} is not on the page at all"
    start = html.rindex("<tr", 0, html.index(needle))
    end = html.index("</tr>", start)
    return html[start:end]


# ── location_name_cache: the station's system id ─────────────────────────────

def test_a_station_carries_its_solar_system_id(client, two_systems):
    """`data-sys-id` is what the distance column keys off in the browser.

    A station whose system never made it into `sys_map` renders `data-sys-id=""`
    and simply never gets a distance — no error, no empty state, just a column
    that stays blank.
    """
    html = client.get("/assets").text

    assert f'data-sys-id="{JITA_SYSTEM}"' in html, (
        "the Jita station lost its solar system id, so the distances column has "
        "nothing to match on")


# There is deliberately **no** test here for the page's `IS NOT NULL` filter.
#
# There was one, and it was vacuous in the exact way this repo keeps rediscovering:
# it opened its own connection, ran its own copy of the SELECT, and asserted on
# that. It would have passed with `assets.py` deleted. What it actually tested
# was SQLite.
#
# Removing it rather than repairing it, because the filter it aimed at is not
# observable from the page and no honest test can be written for it. Measured,
# not assumed: rendering `/assets` with `WHERE solar_system_id IS NOT NULL`
# removed produces **byte-identical HTML**. The reason is that `sys_map` is only
# ever read through `sys_map.get(sid)`, which returns `None` for a missing key
# and `None` for a key whose value is `None`.
#
# The same SELECT in `assets_distances` **is** load-bearing, and
# `test_a_location_with_no_system_gets_no_distance` below covers it — there the
# values become route destinations, so a `None` reaching the todo list is a
# request to `/route/30000142/None/`. One statement, two call sites, redundant
# in one and essential in the other: worth knowing before the conversion treats
# them as the same edit.


# ── /api/assets/distances ────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload


class _FakeESI:
    """Stands in for `esi_client()`: an async context manager whose `get`
    answers by URL. Records the route calls so a test can assert the cache
    spared them."""

    def __init__(self, origin_system: int, jumps: dict[int, int]):
        self.origin_system, self.jumps = origin_system, jumps
        self.routes_asked: list[int] = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        if "/location/" in url:
            return _FakeResponse({"solar_system_id": self.origin_system})
        dest = int(url.rstrip("/").rsplit("/", 1)[-1])
        self.routes_asked.append(dest)
        # ESI returns the systems along the route; the caller takes len - 1.
        return _FakeResponse(list(range(self.jumps.get(dest, 0) + 1)))


@pytest.fixture
def fake_esi(monkeypatch):
    def _install(origin_system: int, jumps: dict[int, int]) -> _FakeESI:
        fake = _FakeESI(origin_system, jumps)
        monkeypatch.setattr(assets_router, "esi_client", fake)
        return fake
    return _install


def test_distances_are_returned_per_location_not_per_system(client, two_systems, fake_esi):
    """The response is keyed on **location**, and the system is the join.

    Two stations in two systems, so a response that answered per system — or
    that answered the same number for both — is visible. With one station it
    would not be.
    """
    fake_esi(JITA_SYSTEM, {AMARR_SYSTEM: 9})

    body = client.get("/api/assets/distances").json()

    assert body["ok"] is True, body
    assert body["origin_sys"] == JITA_SYSTEM
    assert body["distances"][str(JITA)] == 0, "the origin's own station is zero jumps"
    assert body["distances"][str(60008494)] == 9


def test_a_location_with_no_system_gets_no_distance(client, two_systems, fake_esi):
    """The same `IS NOT NULL` filter, seen from the endpoint.

    Without it the unresolved structure joins the todo list as `None`, which is
    a route request to `/route/30000142/None/`.
    """
    fake_esi(JITA_SYSTEM, {AMARR_SYSTEM: 9})

    body = client.get("/api/assets/distances").json()

    assert str(1030000000001) not in body["distances"], (
        "a location whose system is unknown was given a distance")


def test_the_second_call_is_served_from_the_cache(client, two_systems, fake_esi):
    """The reason the cache exists, asserted on the ESI calls rather than on the
    answer — the answer is identical either way, which is exactly why a broken
    cache went unnoticed for as long as it did."""
    first = fake_esi(JITA_SYSTEM, {AMARR_SYSTEM: 9})
    client.get("/api/assets/distances")
    assert AMARR_SYSTEM in first.routes_asked, "the first call should have fetched"

    second = fake_esi(JITA_SYSTEM, {AMARR_SYSTEM: 9})
    body = client.get("/api/assets/distances").json()

    assert second.routes_asked == [], (
        f"the cache did not hold: ESI was asked again for {second.routes_asked}")
    assert body["distances"][str(60008494)] == 9
    assert body["fetched"] == 0
    assert body["from_cache"] > 0


# ── sde_types: the corp container's name ─────────────────────────────────────

def test_the_corp_resolver_names_a_container_from_the_cache(app_module):
    """The corp twin of `test_the_resolver_names_a_container_from_the_cache`,
    which existed only in the negative — the corp side had a test for owning
    nothing and none for the path that actually reads `sde_types`.

    Two containers, one named and one not, so the type-name fallback and the
    custom name are told apart.
    """
    from app.character import assets as assets_api
    from app.db.conn import connect as _connect

    with _connect() as ec:
        assets_api.save_cached_container_names(ec, {222001: "Corp Ammo Bin"})
        ec.commit()

    corp_assets = [
        {"item_id": 222001, "type_id": MEGATHRON, "location_id": JITA},
        {"item_id": 222002, "type_id": BADGER, "location_id": JITA},
    ]
    got = asyncio.run(assets_router._resolve_corp_container_names(
        CORP, "tok", [222001, 222002], corp_assets))

    assert got[222001][0] == "Corp Ammo Bin (Megathron)"
    assert got[222002][0].startswith("Badger"), (
        f"an unnamed corp container lost its type fallback: {got[222002][0]}")
    assert got[222001][1] == JITA, "the parent location came back wrong"


def test_the_corp_resolver_looks_up_only_the_types_it_holds(app_module):
    """Two different hulls, so a query that ignored its `IN` list and returned
    every row in `sde_types` would still have to pick the right name for each —
    which a wrong-keyed result cannot do."""
    corp_assets = [
        {"item_id": 222003, "type_id": MEGATHRON, "location_id": JITA},
        {"item_id": 222004, "type_id": BADGER, "location_id": JITA},
    ]
    got = asyncio.run(assets_router._resolve_corp_container_names(
        CORP, "tok", [222003, 222004], corp_assets))

    assert "Megathron" in got[222003][0]
    assert "Badger" in got[222004][0]
    assert "Megathron" not in got[222004][0], "the two containers got the same type name"


# ── sde_blueprint_products: where the Plan button points ─────────────────────

def test_the_plan_button_points_at_the_product_not_the_blueprint(client, two_blueprints):
    """`product_type_map` is the difference between planning a Bantam and
    planning a Bantam Blueprint. The fallback is `bp.type_id`, so a query that
    returns nothing degrades silently into linking every blueprint to itself.
    """
    html = client.get(f"/blueprints?view={CHAR}").text

    assert f"/plan?product={MANU_PRODUCT}" in html, (
        "the Bantam Blueprint's Plan button does not point at the Bantam — the "
        "product lookup returned nothing and it fell back to the blueprint id")
    assert f"/plan?product={RXN_PRODUCT}" in html, (
        "the reaction formula's product is missing, so 'reaction' is not in the "
        "activity filter")


def test_an_invention_product_is_never_what_the_plan_button_points_at(client, two_blueprints):
    """`activity IN ('manufacturing','reaction')`, pinned so that row order
    cannot decide the outcome.

    **The first version of this test did not catch a dropped filter**, and the
    mutation battery is the only reason that is known. It asserted that the
    Bantam Blueprint's invention output (39581) appeared nowhere — but the
    Bantam also has a `manufacturing` row, so dropping the filter returns both
    and `{r[0]: r[1] for r in prod_rows}` keeps whichever the backend yields
    last. SQLite yielded the manufacturing row last, the map was unchanged, and
    the test passed against code with no filter at all.

    `INVENTION_ONLY_BP` removes the ambiguity: it has twelve product rows and
    **not one** of them is manufacturing or reaction. With the filter it is
    absent from the map and `product_type_map.get(bp.type_id, bp.type_id)`
    falls back to the blueprint's own id. Without it, the map holds one of the
    twelve invention outputs and the fallback never happens — whichever row
    wins. That makes the assertion independent of an ordering the two backends
    are under no obligation to share, which matters because after the
    conversion this test runs on both.
    """
    html = client.get(f"/blueprints?view={CHAR}").text

    assert f"/plan?product={INVENTION_ONLY_BP}" in html, (
        "a blueprint with no manufacturing or reaction product should fall back "
        "to its own type id — instead an invention output got into the map, so "
        "the activity filter is not being applied")
    assert f"/plan?product={INVENTION_PRODUCT}" not in html, (
        f"the Plan button points at {INVENTION_PRODUCT}, an invention output")
