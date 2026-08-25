"""`app/web/routers/locations.py`, before it moves onto the portable query layer.

Eight raw statements and **not one behavioural test** — the routes appear in
`tests/test_cache_only_routes.py`'s exemption list, which proves they exist and
says nothing about what they return.

Written against the `sqlite3` version so the rewrite is judged by whether it
preserves these, the same order of work that found the `_ROUTE_CHUNK` bug in
v0.9.67 and the blind chunking test in v0.9.68.

**`/api/suggest` is the reason this file is careful.** It holds two statements
that each mix an expanding `IN` list with a positional `LIKE ?` — and they do it
in **opposite orders**:

    owned:  ... blueprint_type_id IN ({ph}) ... LIKE ?   params: bp_type_ids + [pattern]
    other:  ... LIKE ? ... type_id NOT IN ({ph2})        params: [pattern] + list(owned)

That is the `search_public_contracts` shape from v0.9.66 exactly: the binding is
a property of the *order the fragments were written in*, and getting it wrong
returns **plausible rows rather than an error**. Named binds make the mistake
unrepresentable, which is why the conversion is worth doing — and why the net
has to go in first, because there is no error to notice.

The SDE ids are chosen so every filter is decidable rather than incidental:

* `hobgoblin` matches **six** manufacturable products, so "returned the right
  one" and "returned all of them" are different observations.
* **`Hobgoblin II Blueprint` is itself a product** — of *invention*, from
  `Hobgoblin I Blueprint`. So `activity IN ('manufacturing','reaction')` has
  something to exclude that matches the same `LIKE`. Without that row the
  activity filter would be invisible here, the way it was in the assets
  blueprint test until `INVENTION_ONLY_BP` was found.
"""
from __future__ import annotations

import json
import time

import pytest

from app.web.routers import locations as loc_router

# ── real SDE ids ─────────────────────────────────────────────────────────────
HOBGOBLIN_I = 2454
HOBGOBLIN_I_BP = 2455
HOBGOBLIN_II = 2456
#: Also the blueprint id of Hobgoblin II — and a *product* of invention, which
#: is what makes the activity filter testable with a name that matches.
HOBGOBLIN_II_BP = 2457
AUGMENTED_HOBGOBLIN = 28276

TRITANIUM = 34

CHAR = 900000001            # the character `client` signs in as
JITA = 60003760
JITA_SYSTEM = 30000142
A_STRUCTURE = 1035000000001


def _bp(type_id: int, item_id: int, *, quantity: int = -1, me: int = 10,
        te: int = 20, runs: int = -1) -> dict:
    """One row shaped like `_load_blueprints_from_cache` returns them.

    `quantity == -1` is what marks an original; anything else is a copy. Getting
    that wrong is how the assets net spent a round badging everything BPC.
    """
    return {
        "item_id": item_id, "type_id": type_id, "location_id": JITA,
        "location_flag": "Hangar", "quantity": quantity,
        "material_efficiency": me, "time_efficiency": te, "runs": runs,
    }


@pytest.fixture
def blueprints(app_module):
    """Install blueprints for the signed-in character and put the cache back.

    The session database is shared with every other route test, so this restores
    what it found rather than leaving Hobgoblins in it.
    """
    installed: list[dict] = []

    def _install(rows: list[dict]) -> None:
        installed.append({})
        conn = app_module.get_conn()
        row = conn.execute(
            "SELECT data_json, cached_at FROM char_blueprints_cache"
            " WHERE character_id=?", (CHAR,)).fetchone()
        installed[0] = {"original": (row[0], row[1]) if row else None}
        conn.execute("DELETE FROM char_blueprints_cache WHERE character_id=?", (CHAR,))
        conn.execute(
            "INSERT INTO char_blueprints_cache (character_id, data_json, cached_at)"
            " VALUES (?,?,?)", (CHAR, json.dumps(rows), time.time()))
        conn.commit()
        conn.close()

    yield _install

    if installed:
        conn = app_module.get_conn()
        conn.execute("DELETE FROM char_blueprints_cache WHERE character_id=?", (CHAR,))
        original = installed[0].get("original")
        if original:
            conn.execute(
                "INSERT INTO char_blueprints_cache (character_id, data_json, cached_at)"
                " VALUES (?,?,?)", (CHAR, original[0], original[1]))
        conn.commit()
        conn.close()


@pytest.fixture
def location_rows(app_module):
    """Clean up anything a test writes to `location_name_cache`."""
    written: set[int] = set()

    def _track(location_id: int) -> int:
        written.add(location_id)
        return location_id

    yield _track

    if written:
        conn = app_module.get_conn()
        for loc_id in written:
            conn.execute("DELETE FROM location_name_cache WHERE location_id=?", (loc_id,))
        conn.commit()
        conn.close()


def _names(entries: list[dict]) -> list[str]:
    return [e["name"] for e in entries]


def _cached_row(location_id: int) -> tuple | None:
    """`(name, solar_system_id)` straight from the cache, or None.

    Read through `app.db.conn` rather than the router's own connection, so the
    assertion is on what landed in the database and not on what the handler
    happened to return.
    """
    from sqlalchemy import text

    from app.db.conn import connect
    with connect() as c:
        row = c.execute(
            text("SELECT name, solar_system_id FROM location_name_cache"
                 " WHERE location_id=:i"), {"i": location_id}).fetchone()
    return tuple(row) if row else None


def _cached_name(location_id: int) -> str | None:
    row = _cached_row(location_id)
    return row[0] if row else None


# ── /api/suggest — the owned half ────────────────────────────────────────────

def test_a_blueprint_you_own_suggests_its_product(client, blueprints):
    """The `owned` list is keyed on the **product**, not the blueprint.

    Searching "hobgoblin" while holding a Hobgoblin I Blueprint should offer
    *Hobgoblin I* — the thing you can build — and carry the blueprint's ME, TE
    and originality along with it.
    """
    blueprints([_bp(HOBGOBLIN_I_BP, 800001, me=8, te=14)])

    body = client.get("/api/suggest?q=hobgoblin").json()

    owned = {e["type_id"]: e for e in body["owned"]}
    assert HOBGOBLIN_I in owned, (
        f"the owned blueprint's product is missing: {_names(body['owned'])}")
    entry = owned[HOBGOBLIN_I]
    assert entry["name"] == "Hobgoblin I"
    assert entry["me"] == 8 and entry["te"] == 14
    assert entry["is_original"] is True
    assert entry["runs"] == "∞", "a BPO has unlimited runs and renders as ∞"


def test_a_copy_is_not_reported_as_an_original(client, blueprints):
    """`quantity == -1` is the only thing separating a BPO from a BPC, and a BPC
    carries a finite run count that the plan form needs."""
    blueprints([_bp(HOBGOBLIN_I_BP, 800002, quantity=1, runs=42)])

    body = client.get("/api/suggest?q=hobgoblin").json()

    entry = next(e for e in body["owned"] if e["type_id"] == HOBGOBLIN_I)
    assert entry["is_original"] is False
    assert entry["runs"] == 42


def test_the_best_copy_of_a_blueprint_wins(client, blueprints):
    """Two blueprints for the same product: BPO beats BPC, and higher ME wins.

    Two of the *same* type is the smallest fixture that can tell "picked the
    best" from "picked the first".
    """
    blueprints([
        _bp(HOBGOBLIN_I_BP, 800003, quantity=1, me=2, runs=10),   # a poor copy
        _bp(HOBGOBLIN_I_BP, 800004, quantity=-1, me=9),           # the original
    ])

    body = client.get("/api/suggest?q=hobgoblin").json()

    matches = [e for e in body["owned"] if e["type_id"] == HOBGOBLIN_I]
    assert len(matches) == 1, f"the product was offered twice: {matches}"
    assert matches[0]["is_original"] is True
    assert matches[0]["me"] == 9


def test_owning_one_blueprint_does_not_suggest_every_product(client, blueprints):
    """The `IN` list is what scopes the owned half to what you actually hold.

    Six products match "hobgoblin"; holding one blueprint must offer exactly
    one. If the `IN` list and the `LIKE` pattern were bound to each other's
    placeholders, this is where it shows.
    """
    blueprints([_bp(HOBGOBLIN_I_BP, 800005)])

    body = client.get("/api/suggest?q=hobgoblin").json()

    assert _names(body["owned"]) == ["Hobgoblin I"], (
        f"expected only the product of the one blueprint held: "
        f"{_names(body['owned'])}")


# ── /api/suggest — the other half, and the NOT IN between them ───────────────

def test_products_you_do_not_own_come_back_as_other(client, blueprints):
    blueprints([_bp(HOBGOBLIN_I_BP, 800006)])

    body = client.get("/api/suggest?q=hobgoblin").json()

    other = _names(body["other"])
    assert "Hobgoblin II" in other, f"the rest of the family is missing: {other}"
    assert "'Augmented' Hobgoblin" in other


def test_a_product_you_own_is_not_repeated_in_other(client, blueprints):
    """The `NOT IN` that keeps the two lists disjoint. The form renders them as
    separate groups, so a product in both is offered twice."""
    blueprints([_bp(HOBGOBLIN_I_BP, 800007)])

    body = client.get("/api/suggest?q=hobgoblin").json()

    assert "Hobgoblin I" in _names(body["owned"])
    assert "Hobgoblin I" not in _names(body["other"]), (
        "the owned product also appeared under other — the NOT IN exclusion is "
        "not being applied")


@pytest.mark.parametrize("held", [
    pytest.param([_bp(HOBGOBLIN_I_BP, 800008)], id="holding-a-blueprint"),
    pytest.param([], id="holding-nothing"),
])
def test_an_invention_product_is_never_suggested(client, blueprints, held):
    """`activity IN ('manufacturing','reaction')`.

    `Hobgoblin II Blueprint` matches the search term and **is** a product row —
    of invention, from Hobgoblin I Blueprint. Suggesting it would put a
    blueprint into a picker whose whole job is to choose a thing to build.

    **Run both ways because `other` is two different statements.** Owning
    something takes the branch with the `NOT IN`; owning nothing takes a
    separate query that has no exclusion list — and each carries its own copy of
    the activity filter. The single-branch version of this test missed a
    mutation that dropped the filter from the second one, which is the
    "two of the thing being tested" rule applied to *branches* rather than rows.
    """
    blueprints(held)

    body = client.get("/api/suggest?q=hobgoblin").json()

    everything = _names(body["owned"]) + _names(body["other"])
    assert everything, "the search returned nothing, so this proves nothing"
    assert "Hobgoblin II Blueprint" not in everything, (
        f"an invention product was suggested: {everything}")


def test_the_search_term_actually_filters(client, blueprints):
    """Both statements carry the `LIKE`, and it is the one parameter they share
    with the `IN` lists. A search that matched everything would still look
    plausible on a page that only ever shows fifteen rows."""
    blueprints([_bp(HOBGOBLIN_I_BP, 800009)])

    body = client.get("/api/suggest?q=warrior").json()

    assert body["owned"] == [], (
        f"a Hobgoblin blueprint was offered for a Warrior search: {body['owned']}")
    other = _names(body["other"])
    assert other, "the search found nothing at all"
    assert all("warrior" in n.lower() for n in other), (
        f"names that do not match the term came back: {other}")


def test_the_search_works_with_no_blueprints_at_all(client, blueprints):
    """The third statement — the `else` branch, which has no `NOT IN` and takes
    the pattern alone. Nothing else in this file reaches it."""
    blueprints([])

    body = client.get("/api/suggest?q=hobgoblin").json()

    assert body["owned"] == []
    assert "Hobgoblin I" in _names(body["other"]), (
        "with nothing owned, every match belongs under other")


def test_a_one_character_search_asks_the_database_nothing(client):
    """The `len(q.strip()) < 2` guard. An autocomplete that fires on the first
    keystroke runs a `LIKE '%a%'` across the whole type table."""
    assert client.get("/api/suggest?q=h").json() == {"owned": [], "other": []}


# ── location_name_cache: the upserts ─────────────────────────────────────────

def test_renaming_a_location_stores_the_name(client, location_rows):
    loc = location_rows(A_STRUCTURE)

    body = client.post("/api/location/rename",
                       json={"location_id": loc, "name": "Home Base"}).json()

    assert body["ok"] is True
    assert _cached_name(loc) == "Home Base"


def test_renaming_does_not_wipe_the_solar_system(client, location_rows, app_module):
    """The rename statement sets **only** `name` on conflict, and that is
    deliberate: the system id is what `/assets` uses to compute distances, and
    the rename form has no idea what it is.

    A conversion that "tidied" this into the same three-column upsert the other
    three sites use would null the system on every rename — silently, and only
    visible later as a distance column that stopped working.
    """
    loc = location_rows(A_STRUCTURE)
    conn = app_module.get_conn()
    conn.execute("INSERT OR REPLACE INTO location_name_cache"
                 " (location_id, name, solar_system_id) VALUES (?,?,?)",
                 (loc, "Old Name", JITA_SYSTEM))
    conn.commit()
    conn.close()

    client.post("/api/location/rename", json={"location_id": loc, "name": "New Name"})

    row = _cached_row(loc)

    assert row[0] == "New Name"
    assert row[1] == JITA_SYSTEM, (
        "the rename cleared the solar system id — /assets distances silently "
        "stop working for this location")


def test_an_empty_rename_is_refused(client):
    """Falling through would store an empty name, and the display falls back to
    the raw id only when the row is *absent*, not when it is blank."""
    body = client.post("/api/location/rename",
                       json={"location_id": A_STRUCTURE, "name": "   "}).json()

    assert body["ok"] is False


# ── market_price_cache: the seed that must not overwrite ─────────────────────

@pytest.fixture
def stub_market(monkeypatch):
    """Stand in for the region lookup and the station-volume fetch.

    Both reach ESI, and neither is what these tests are about — what matters is
    the row `fetch_plan_sell_price` writes *before* either of them runs.
    """
    async def _region(location_id, token):
        return 10000002

    async def _volumes(conn, location_id, region_id, type_ids):
        return {tid: (None, 123.0, None) for tid in type_ids}

    monkeypatch.setattr(loc_router, "_region_for", _region)
    monkeypatch.setattr(loc_router, "fetch_station_volumes", _volumes)


def _price_row(type_id: int) -> tuple | None:
    from sqlalchemy import text

    from app.db.conn import connect
    with connect() as c:
        row = c.execute(
            text("SELECT sell_price, buy_price FROM market_price_cache"
                 " WHERE type_id=:t"), {"t": type_id}).fetchone()
    return tuple(row) if row else None


def test_fetching_a_price_seeds_a_type_the_cache_has_never_seen(client, stub_market):
    """The row has to exist before the fetchers run — they filter on it — so an
    absent type is inserted with NULL prices rather than skipped."""
    from sqlalchemy import text

    from app.db.conn import connect
    with connect() as c:
        c.execute(text("DELETE FROM market_price_cache WHERE type_id=:t"),
                  {"t": HOBGOBLIN_I})
        c.commit()

    body = client.get(
        f"/api/plan/fetch-sell-price?location_id={JITA}&type_id={HOBGOBLIN_I}").json()

    assert body["ok"] is True
    assert _price_row(HOBGOBLIN_I) is not None, (
        "the type was never seeded, so the fetchers' filter would skip it")


def test_fetching_a_price_never_overwrites_one_already_cached(client, stub_market):
    """`ON CONFLICT (type_id) DO NOTHING`, and the DO NOTHING is the whole point.

    The statement exists only to guarantee the row is present. Written as
    `DO UPDATE` it would write its own NULL placeholders over a real price
    **every time the plan form asked for a quote** — a cache wipe with no error
    and no symptom until a page reported nothing for an item it knew yesterday.

    Tritanium is seeded with a real price by `conftest`, so there is something
    here to destroy.
    """
    before = _price_row(TRITANIUM)
    assert before is not None and before[0] is not None, (
        "the fixture has no cached price, so this test could not detect a wipe")

    client.get(f"/api/plan/fetch-sell-price?location_id={JITA}&type_id={TRITANIUM}")

    assert _price_row(TRITANIUM) == before, (
        "the seed overwrote a cached price — DO NOTHING has become DO UPDATE")
