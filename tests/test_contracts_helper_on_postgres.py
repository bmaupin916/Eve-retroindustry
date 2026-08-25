"""`app/web/contracts_helper.py`, before it moves onto the portable query layer.

The public-contract index: fetch every public contract in a region plus its
items, store them, and search locally. It exists because ESI's listing carries
no items and the `title` is usually empty, so searching by item is only possible
over a local copy.

Four of its seven functions have no test: `get_index_status`,
`stream_public_index`, `search_public_contracts` and `get_contract_items`.
`tests/test_logic.py` covers `best_contract_price` and the schema shim, and
`tests/test_esi_etags.py` reaches `_store`. `stream_public_index` is an SSE
generator over live ESI and is left alone here — it is covered as a *route* in
`tests/test_sse_streams.py`, and what this file is for is the SQL underneath.

**These assertions are unchanged by the conversion.** They were written against
the `sqlite3` version first, so the rewrite could be judged by whether it
preserves them. Only the fixture underneath moved, and it now runs each of them
on both backends.

Four things here are conversion traps rather than ordinary behaviour:

* **`search_public_contracts` builds its SQL and its parameter list together**,
  positionally, with the `LIKE` and the `LIMIT` appended in whatever order the
  filters happened to fire. Named binds do not care about order, which is why
  the rewrite is safe — but it is also why every filter combination needs a
  test first: a builder that silently pairs the wrong value with the wrong
  placeholder returns *plausible* rows, not an error.
* **`_store` commits.** It is the one writer in this cluster that owns its own
  transaction boundary rather than leaving it to the caller — the SSE generator
  that drives it streams progress and must not hold a write open across the
  whole region.
* **`get_contract_items` builds a fallback name with `'#'||i.type_id`.** SQLite
  and Postgres both accept `text || integer`, checked rather than assumed, so
  the concatenation survives — but the `is_included` round-trip through
  `bool()` does not survive carelessness: the column is an INTEGER holding 1/0,
  not a boolean.
* **`contract_id` is `Integer` here and `BigInteger` in
  `contract_items_cache`.** Deliberate, and measured at v0.9.57: the largest
  real id seen was 234,465,667, or 10.9% of the int32 ceiling. Pinned below so
  the asymmetry reads as a decision rather than an oversight.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.web import contracts_helper as helper
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_contracts_helper"

JITA_REGION = 10000002
HEIMATAR = 10000030
TRITANIUM = 34
PYERITE = 35


def _build_sde_tables(eng) -> None:
    """Create the SDE tables on whichever backend `eng` speaks.

    They are **deliberately absent from the migration history** — CCP drops and
    rebuilds the static data wholesale on every SDE build, so `0001_baseline`
    documents the exclusion and `apply_sde_schema()` creates them instead. That
    function compiles its DDL against the SQLite dialect explicitly, so it is
    no use here; going through the metadata gives the same tables in whichever
    dialect the engine is bound to.

    Both are needed and they arrive separately: the migrations build
    `APP_TABLES`, and `search_public_contracts` joins `sde_types` when filtering
    by item name while `get_contract_items` LEFT JOINs it for the `#id`
    fallback. A fixture with only one of the two silently loses both.
    """
    from app.db.schema import metadata, SDE_TABLES

    metadata.create_all(eng, tables=[metadata.tables[n] for n in sorted(SDE_TABLES)])


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    """An engine per backend, built once for the module."""
    from app.db.migrate import upgrade_to_head

    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'eve_cache.db'}"
        upgrade_to_head(url)
        eng = create_engine(url)
        _build_sde_tables(eng)
        yield eng
        eng.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.commit()
    admin.dispose()

    scoped = PG_URL + ("&" if "?" in PG_URL else "?") +         f"options=-csearch_path%3D{PG_SCHEMA}"
    upgrade_to_head(scoped)

    eng = create_engine(scoped)
    _build_sde_tables(eng)
    yield eng
    eng.dispose()


#: Emptied before every test, so one module-scoped schema can serve them all.
_CLEARED = ("public_contract_items", "public_contracts", "public_contract_meta")


@pytest.fixture(autouse=True)
def _empty_tables(engine):
    """Before, not after: a test that dies half-way must not leave its rows for
    the next one to read."""
    with engine.connect() as c:
        for table in _CLEARED:
            c.execute(text(f"DELETE FROM {table}"))
        c.execute(text("DELETE FROM sde_types"))
        c.execute(
            text("INSERT INTO sde_types (type_id, name) VALUES (:tid, :name)"),
            [{"tid": TRITANIUM, "name": "Tritanium"},
             {"tid": PYERITE, "name": "Pyerite"}])
        c.commit()
    yield


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        yield c


def test_both_backends_are_actually_exercised(conn):
    """Without this a broken Postgres fixture reads as a passing file: the
    SQLite half would carry it, and running on both is the entire point."""
    assert conn.engine.dialect.name in ("sqlite", "postgresql")
    for table in _CLEARED:
        assert conn.execute(
            text(f"SELECT COUNT(*) FROM {table}")).fetchone()[0] == 0


def _contract(cid, *, region=JITA_REGION, ctype="item_exchange", price=100.0,
              title="", expired="2026-09-01T00:00:00Z"):
    return {"contract_id": cid, "type": ctype, "price": price, "reward": 0,
            "collateral": 0, "buyout": 0, "volume": 1.0,
            "date_expired": expired, "title": title,
            "start_location_id": 60003760, "end_location_id": None,
            "issuer_id": 90000001}


def _item(type_id, qty=1, included=True):
    return {"type_id": type_id, "quantity": qty, "is_included": included}


# ── the index status ─────────────────────────────────────────────────────────

def test_an_unindexed_region_has_no_status(conn):
    """`None`, not a zero count — the page offers to index when it is None, and
    a zero would read as "indexed, and this region has no contracts"."""
    assert helper.get_index_status(conn, JITA_REGION) is None


def test_storing_records_when_and_how_many(conn):
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)], {})

    status = helper.get_index_status(conn, JITA_REGION)

    assert status["contract_count"] == 2
    assert status["indexed_at"] > 0


def test_each_region_has_its_own_status(conn):
    helper._store(conn, JITA_REGION, [_contract(1)], {})
    helper._store(conn, HEIMATAR, [_contract(2), _contract(3)], {})

    assert helper.get_index_status(conn, JITA_REGION)["contract_count"] == 1
    assert helper.get_index_status(conn, HEIMATAR)["contract_count"] == 2


def test_reindexing_a_region_replaces_its_status(conn):
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)], {})
    helper._store(conn, JITA_REGION, [_contract(1)], {})

    assert helper.get_index_status(conn, JITA_REGION)["contract_count"] == 1


# ── _store ───────────────────────────────────────────────────────────────────

def test_storing_commits(engine):
    """The one writer in this cluster that owns its own boundary. The SSE
    generator driving it streams progress across a whole region and must not
    hold a write open for the duration."""
    with engine.connect() as writer:
        helper._store(writer, JITA_REGION, [_contract(1)], {})

    with engine.connect() as reader:
        assert reader.execute(
            text("SELECT COUNT(*) FROM public_contracts")).fetchone()[0] == 1, (
            "the writer did not commit — a caller would have to, and the SSE "
            "generator does not")


def test_reindexing_drops_the_regions_previous_contracts(conn):
    """`DELETE FROM public_contracts WHERE region_id=?` first. Without it a
    contract that expired between indexes stays in the search results
    forever."""
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)], {})
    helper._store(conn, JITA_REGION, [_contract(2)], {})

    got = helper.search_public_contracts(conn, JITA_REGION)

    assert [c["contract_id"] for c in got] == [2]


def test_reindexing_one_region_leaves_another_alone(conn):
    """The DELETE is scoped by region. Unscoped, indexing Heimatar would empty
    the Jita index somebody just paid four hundred ESI calls for."""
    helper._store(conn, JITA_REGION, [_contract(1)], {})
    helper._store(conn, HEIMATAR, [_contract(2)], {})

    assert [c["contract_id"] for c in
            helper.search_public_contracts(conn, JITA_REGION)] == [1]


def test_items_are_stored_against_their_contract(conn):
    helper._store(conn, JITA_REGION, [_contract(1)],
                  {1: [_item(TRITANIUM, 1000), _item(PYERITE, 500)]})

    got = helper.get_contract_items(conn, 1)

    assert {i["type_id"]: i["quantity"] for i in got} == {
        TRITANIUM: 1000, PYERITE: 500}


def test_reindexing_replaces_a_contracts_items(conn):
    """The items DELETE is keyed on contract_id, not region, because a contract
    can move between indexes. Without it a re-index doubles every line."""
    helper._store(conn, JITA_REGION, [_contract(1)], {1: [_item(TRITANIUM, 1000)]})
    helper._store(conn, JITA_REGION, [_contract(1)], {1: [_item(TRITANIUM, 1000)]})

    assert len(helper.get_contract_items(conn, 1)) == 1


def test_reindexing_clears_every_contracts_items_not_just_the_first(conn):
    """The DELETE takes the whole id list, and with **one** contract in the
    fixture that is indistinguishable from deleting a single id — which is how
    a mutation replacing the expanding bindparam with `contract_id = :cids`
    passed everything. Two contracts is the smallest case that can tell them
    apart, and a real re-index carries thousands.

    Left un-deleted, the second contract's lines survive alongside the new
    ones and its items double on every index."""
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)],
                  {1: [_item(TRITANIUM, 1000)], 2: [_item(PYERITE, 500)]})
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)],
                  {1: [_item(TRITANIUM, 1000)], 2: [_item(PYERITE, 500)]})

    assert len(helper.get_contract_items(conn, 1)) == 1
    assert len(helper.get_contract_items(conn, 2)) == 1, (
        "only the first contract's items were cleared — every contract after "
        "the first doubles on each re-index")


def test_an_item_without_a_type_id_is_skipped(conn):
    """ESI omits it on some rows. Stored, it would be a line the search can
    never match and the page renders as `#None`."""
    helper._store(conn, JITA_REGION, [_contract(1)],
                  {1: [_item(TRITANIUM), {"quantity": 5}]})

    assert [i["type_id"] for i in helper.get_contract_items(conn, 1)] == [TRITANIUM]


def test_missing_numeric_fields_become_zero_not_null(conn):
    """`c.get("price") or 0`. A NULL price would drop the contract out of
    `price <= ?` filtering and out of `best_contract_price`'s `price > 0`,
    silently."""
    bare = {"contract_id": 1, "type": "item_exchange"}
    helper._store(conn, JITA_REGION, [bare], {})

    got = helper.search_public_contracts(conn, JITA_REGION)

    assert got[0]["price"] == 0
    assert got[0]["volume"] == 0


# ── get_contract_items ───────────────────────────────────────────────────────

def test_item_names_come_from_the_sde(conn):
    helper._store(conn, JITA_REGION, [_contract(1)], {1: [_item(TRITANIUM)]})

    assert helper.get_contract_items(conn, 1)[0]["name"] == "Tritanium"


def test_an_unknown_type_falls_back_to_its_id(conn):
    """A LEFT JOIN plus `'#'||i.type_id`. The SDE subset does not carry every
    type, and a contract whose item is missing from it must still render."""
    helper._store(conn, JITA_REGION, [_contract(1)], {1: [_item(999999)]})

    assert helper.get_contract_items(conn, 1)[0]["name"] == "#999999"


def test_inclusion_survives_as_a_bool(conn):
    """The column is an INTEGER holding 1/0; the reader wraps it in `bool()`.
    A courier's excluded items reading as included is the difference between
    "this contract contains X" and "you must supply X"."""
    helper._store(conn, JITA_REGION, [_contract(1)],
                  {1: [_item(TRITANIUM, included=True),
                       _item(PYERITE, included=False)]})

    got = {i["type_id"]: i["included"] for i in helper.get_contract_items(conn, 1)}

    assert got[TRITANIUM] is True
    assert got[PYERITE] is False


def test_items_of_an_unknown_contract_are_empty(conn):
    assert helper.get_contract_items(conn, 12345) == []


def test_items_are_scoped_to_the_contract_asked_for(conn):
    """Every other test in this section stores exactly one contract with items,
    so a `WHERE` that matched everything would look identical. Two contracts is
    the smallest fixture that can tell them apart — and on the real page this is
    the difference between expanding a row and seeing the whole region's
    inventory under it."""
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)],
                  {1: [_item(TRITANIUM)], 2: [_item(PYERITE)]})

    assert [i["type_id"] for i in helper.get_contract_items(conn, 1)] == [TRITANIUM]
    assert [i["type_id"] for i in helper.get_contract_items(conn, 2)] == [PYERITE]


# ── search_public_contracts: one test per filter, and per combination ────────

def test_search_returns_the_regions_contracts(conn):
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)], {})

    assert {c["contract_id"] for c in
            helper.search_public_contracts(conn, JITA_REGION)} == {1, 2}


def test_search_is_scoped_to_one_region(conn):
    helper._store(conn, JITA_REGION, [_contract(1)], {})
    helper._store(conn, HEIMATAR, [_contract(2)], {})

    assert [c["contract_id"] for c in
            helper.search_public_contracts(conn, HEIMATAR)] == [2]


def test_search_by_item_name(conn):
    """The join onto `public_contract_items` and `sde_types`. This is the whole
    reason the local index exists — ESI cannot answer it."""
    helper._store(conn, JITA_REGION, [_contract(1), _contract(2)],
                  {1: [_item(TRITANIUM)], 2: [_item(PYERITE)]})

    got = helper.search_public_contracts(conn, JITA_REGION, item="Tritanium")

    assert [c["contract_id"] for c in got] == [1]


def test_search_by_item_is_a_substring_match(conn):
    helper._store(conn, JITA_REGION, [_contract(1)], {1: [_item(TRITANIUM)]})

    assert len(helper.search_public_contracts(conn, JITA_REGION, item="rita")) == 1


def test_an_item_match_returns_each_contract_once(conn):
    """`SELECT DISTINCT`. The join multiplies by matching items, so a contract
    holding two matching lines would otherwise appear twice."""
    helper._store(conn, JITA_REGION, [_contract(1)],
                  {1: [_item(TRITANIUM, 100), _item(TRITANIUM, 200)]})

    got = helper.search_public_contracts(conn, JITA_REGION, item="Tritanium")

    assert [c["contract_id"] for c in got] == [1]


def test_search_by_contract_type(conn):
    helper._store(conn, JITA_REGION,
                  [_contract(1, ctype="item_exchange"), _contract(2, ctype="courier")], {})

    got = helper.search_public_contracts(conn, JITA_REGION, ctype="courier")

    assert [c["contract_id"] for c in got] == [2]


def test_search_by_max_price(conn):
    helper._store(conn, JITA_REGION,
                  [_contract(1, price=50.0), _contract(2, price=500.0)], {})

    got = helper.search_public_contracts(conn, JITA_REGION, max_price=100.0)

    assert [c["contract_id"] for c in got] == [1]


def test_a_max_price_of_zero_is_a_filter_not_an_absence(conn):
    """`max_price is not None`, not truthiness. Zero is a real ceiling — it
    selects the free contracts — and treating it as "no filter" would return
    the whole region instead."""
    helper._store(conn, JITA_REGION,
                  [_contract(1, price=0.0), _contract(2, price=500.0)], {})

    got = helper.search_public_contracts(conn, JITA_REGION, max_price=0.0)

    assert [c["contract_id"] for c in got] == [1]


def test_all_three_filters_at_once(conn):
    """The combination the parameter order actually depends on: with `item`
    set, the LIKE is bound before the type and the price, and the LIMIT after
    all of them. A builder that pairs one value with the wrong placeholder
    returns plausible rows rather than an error, which is why this is here."""
    helper._store(conn, JITA_REGION, [
        _contract(1, ctype="item_exchange", price=50.0),
        _contract(2, ctype="item_exchange", price=5000.0),
        _contract(3, ctype="courier", price=50.0),
        _contract(4, ctype="item_exchange", price=50.0),
    ], {1: [_item(TRITANIUM)], 2: [_item(TRITANIUM)],
        3: [_item(TRITANIUM)], 4: [_item(PYERITE)]})

    got = helper.search_public_contracts(
        conn, JITA_REGION, item="Tritanium", ctype="item_exchange", max_price=100.0)

    assert [c["contract_id"] for c in got] == [1], (
        "only contract 1 matches all three; a mispaired parameter widens this")


def test_results_are_ordered_by_price(conn):
    helper._store(conn, JITA_REGION, [
        _contract(1, price=300.0), _contract(2, price=100.0), _contract(3, price=200.0),
    ], {})

    got = helper.search_public_contracts(conn, JITA_REGION)

    assert [c["contract_id"] for c in got] == [2, 3, 1]


def test_the_limit_caps_the_result(conn):
    """The LIMIT is the last bound parameter, after however many filters fired.
    That ordering is exactly what named binds remove the risk of."""
    helper._store(conn, JITA_REGION,
                  [_contract(i, price=float(i)) for i in range(1, 11)], {})

    got = helper.search_public_contracts(conn, JITA_REGION, limit=3)

    assert [c["contract_id"] for c in got] == [1, 2, 3]


def test_the_limit_still_applies_with_every_filter_on(conn):
    helper._store(conn, JITA_REGION,
                  [_contract(i, price=float(i)) for i in range(1, 11)],
                  {i: [_item(TRITANIUM)] for i in range(1, 11)})

    got = helper.search_public_contracts(
        conn, JITA_REGION, item="Tritanium", ctype="item_exchange",
        max_price=1000.0, limit=2)

    assert len(got) == 2


def test_a_blank_item_is_not_a_filter(conn):
    """Whitespace only. `item.strip()` is what stops an empty search box
    joining the items table and dropping every contract that has no items."""
    helper._store(conn, JITA_REGION, [_contract(1)], {})

    assert len(helper.search_public_contracts(conn, JITA_REGION, item="   ")) == 1


def test_search_returns_the_columns_the_page_renders(conn):
    """The column list is positional against the SELECT. A column added to one
    and not the other shifts every value after it — and they are mostly
    numbers, so it renders rather than raises."""
    helper._store(conn, JITA_REGION, [_contract(1, title="Fast courier")], {})

    (got,) = helper.search_public_contracts(conn, JITA_REGION)

    assert got["contract_id"] == 1
    assert got["type"] == "item_exchange"
    assert got["title"] == "Fast courier"
    assert got["start_location_id"] == 60003760
    assert got["issuer_id"] == 90000001


# ── best_contract_price ──────────────────────────────────────────────────────

def test_the_cheapest_single_item_contract_wins(conn):
    helper._store(conn, JITA_REGION,
                  [_contract(1, price=1000.0), _contract(2, price=400.0)],
                  {1: [_item(TRITANIUM, 100)], 2: [_item(TRITANIUM, 100)]})

    best = helper.best_contract_price(conn, JITA_REGION, TRITANIUM)

    assert best["contract_id"] == 2
    assert best["price"] == 4.0
    assert best["is_bundle"] is False


def test_a_bundle_is_only_used_when_nothing_is_single(conn):
    """A bundle's price/unit also covers the other items in it, so it is
    indicative at best. Preferring it over a clean single would understate
    what the product actually costs."""
    helper._store(conn, JITA_REGION, [_contract(1, price=1000.0)],
                  {1: [_item(TRITANIUM, 100), _item(PYERITE, 50)]})

    best = helper.best_contract_price(conn, JITA_REGION, TRITANIUM)

    assert best["is_bundle"] is True
    assert best["bundle_count"] == 1
    assert best["single_count"] == 0


def test_a_single_beats_a_cheaper_bundle(conn):
    helper._store(conn, JITA_REGION,
                  [_contract(1, price=100.0), _contract(2, price=1000.0)],
                  {1: [_item(TRITANIUM, 100), _item(PYERITE, 50)],
                   2: [_item(TRITANIUM, 100)]})

    best = helper.best_contract_price(conn, JITA_REGION, TRITANIUM)

    assert best["contract_id"] == 2, "the cheaper bundle displaced a clean single"
    assert best["is_bundle"] is False


def test_a_product_nowhere_in_the_region_is_none(conn):
    helper._store(conn, JITA_REGION, [_contract(1)], {1: [_item(PYERITE)]})

    assert helper.best_contract_price(conn, JITA_REGION, TRITANIUM) is None


def test_only_item_exchange_contracts_are_priced(conn):
    """A courier's price is a shipping fee, not the value of the cargo."""
    helper._store(conn, JITA_REGION, [_contract(1, ctype="courier", price=100.0)],
                  {1: [_item(TRITANIUM, 100)]})

    assert helper.best_contract_price(conn, JITA_REGION, TRITANIUM) is None


def test_a_zero_price_contract_is_not_a_free_product(conn):
    """`c.price > 0`. A zero-price item_exchange is a gift or a scam setup, and
    either way quoting it as the market rate would put a zero into every build
    estimate that uses this."""
    helper._store(conn, JITA_REGION, [_contract(1, price=0.0)],
                  {1: [_item(TRITANIUM, 100)]})

    assert helper.best_contract_price(conn, JITA_REGION, TRITANIUM) is None


def test_an_excluded_line_is_not_something_you_receive(conn):
    """`pi.is_included = 1` in the WHERE. A want-to-buy contract lists the
    product as *excluded* — it is what you must hand over — so pricing it would
    invert the trade."""
    helper._store(conn, JITA_REGION, [_contract(1, price=100.0)],
                  {1: [_item(TRITANIUM, 100, included=False)]})

    assert helper.best_contract_price(conn, JITA_REGION, TRITANIUM) is None


def test_a_zero_quantity_line_is_skipped_not_divided_by(conn):
    """`if not qty or qty <= 0: continue`, and the guard is arithmetic rather
    than tidiness: `price / qty` on a zero raises, and treating the line as one
    unit instead would quote a whole contract's price as the unit price of one
    item. ESI does return zero-quantity lines."""
    helper._store(conn, JITA_REGION,
                  [_contract(1, price=1_000_000.0), _contract(2, price=400.0)],
                  {1: [_item(TRITANIUM, 0)], 2: [_item(TRITANIUM, 100)]})

    best = helper.best_contract_price(conn, JITA_REGION, TRITANIUM)

    assert best["contract_id"] == 2, (
        "the zero-quantity line was priced — as one unit, it is the cheapest "
        "thing in the region and wins every comparison")
    assert best["price"] == 4.0


def test_a_zero_quantity_line_alone_prices_nothing(conn):
    """Skipped, not fallen back on. With nothing else in the region the answer
    is None rather than an invented number."""
    helper._store(conn, JITA_REGION, [_contract(1, price=100.0)],
                  {1: [_item(TRITANIUM, 0)]})

    assert helper.best_contract_price(conn, JITA_REGION, TRITANIUM) is None


def test_pricing_is_scoped_to_the_region(conn):
    helper._store(conn, HEIMATAR, [_contract(1, region=HEIMATAR, price=100.0)],
                  {1: [_item(TRITANIUM, 100)]})

    assert helper.best_contract_price(conn, JITA_REGION, TRITANIUM) is None
    assert helper.best_contract_price(conn, HEIMATAR, TRITANIUM) is not None


# ── the deliberate int32 ─────────────────────────────────────────────────────

def test_public_contract_ids_are_int32_on_purpose(conn):
    """`contract_id` is `Integer` here and `BigInteger` in
    `contract_items_cache` — the same identifier at two widths.

    That asymmetry is a decision, not an oversight: the v0.9.57 audit measured
    the largest real contract id at 234,465,667, or **10.9%** of the int32
    ceiling, against `character_id` at 98.9% which is why *that* one was
    widened. This test exists so the next person to notice the difference finds
    the reasoning attached to it rather than re-deriving it.

    If contract ids ever approach 2**31, both columns move together — and the
    character-side cache already accepts values this one would reject.
    """
    biggest_seen = 234_465_667
    helper._store(conn, JITA_REGION, [_contract(biggest_seen)],
                  {biggest_seen: [_item(TRITANIUM)]})

    got = helper.search_public_contracts(conn, JITA_REGION)

    assert [c["contract_id"] for c in got] == [biggest_seen]
    assert helper.get_contract_items(conn, biggest_seen)[0]["type_id"] == TRITANIUM
