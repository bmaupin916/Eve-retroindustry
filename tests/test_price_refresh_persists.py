"""Refreshing prices must actually write them.

Both bulk-persist functions build a `rows` list and hand it to `execute()` with
named binds. Step 4's conversion (v0.9.72) turned the has-orders branch into a
dict and **left the no-orders branch a tuple**, and SQLAlchemy refuses a list
that mixes the two: *"List argument must consist only of dictionaries"*.

`wanted` is the app's whole tradeable set, so at least one of ~19,000 types
always has no order in the region. The bad branch was therefore taken on every
refresh, every refresh raised, and:

* `market_price_cache` in the development database was last written
  **2026-08-18** — before the conversion — and could not be renewed since.
* `hub_price_cache` has **zero** rows, which is why the reactions board's Sell
  Advantage column has never once shown a number and why §9.4's regional-edge
  KPI had nothing to compute from. Both were filed as separate gaps; they were
  one bug.

Nothing failed loudly because every page reads from the cache and the cache
still had ten-day-old data in it. A stale number looks exactly like a fresh one.

The tests below drive the two functions with a `wanted` set larger than `bulk`,
which is the ordinary case and was the broken one.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.web.prices_helper import _persist_bulk_orders, _persist_hub_bulk_orders

REGION = 10000002
TRADED = 34          # has orders
UNTRADED = 999_111   # in `wanted`, absent from `bulk` — the branch that raised


@pytest.fixture
def conn(tmp_path):
    from app.db.schema import apply_schema

    eng = create_engine(f"sqlite:///{tmp_path / 'eve_cache.db'}")
    with eng.connect() as c:
        apply_schema(c.connection.driver_connection)
        yield c
    eng.dispose()


BULK = {TRADED: {"sell": 5.0, "buy": 4.0, "available": 100}}
WANTED = {TRADED, UNTRADED}


def test_jita_prices_persist_when_some_types_have_no_orders(conn):
    """The ordinary case: more types wanted than the region trades."""
    refreshed, traded = _persist_bulk_orders(conn, BULK, WANTED)

    assert refreshed == 1
    assert traded == [TRADED]
    rows = dict(conn.execute(
        text("SELECT type_id, sell_price FROM market_price_cache")).fetchall())
    assert rows[TRADED] == pytest.approx(5.0)
    # The untraded type is written explicitly as "no price", which is a
    # different statement from "not refreshed yet".
    assert UNTRADED in rows and rows[UNTRADED] is None


def test_hub_prices_persist_when_some_types_have_no_orders(conn):
    """Same shape, and this one had never written a single row."""
    refreshed, traded = _persist_hub_bulk_orders(conn, REGION, BULK, WANTED)

    assert refreshed == 1
    assert traded == [TRADED]
    rows = dict(conn.execute(
        text("SELECT type_id, sell_price FROM hub_price_cache"
             " WHERE region_id = :r"), {"r": REGION}).fetchall())
    assert rows[TRADED] == pytest.approx(5.0)
    assert UNTRADED in rows and rows[UNTRADED] is None


def test_a_refresh_with_nothing_traded_still_writes(conn):
    """Every type unpriced is the extreme of the same case.

    A hub the user has never fetched starts here, so if this raises the cache
    can never leave zero rows — which is exactly what happened.
    """
    refreshed, traded = _persist_hub_bulk_orders(conn, REGION, {}, WANTED)

    assert refreshed == 0 and traded == []
    assert conn.execute(
        text("SELECT COUNT(*) FROM hub_price_cache")).scalar() == len(WANTED)


def test_no_row_list_mixes_tuples_and_dicts():
    """The lint for the bug class, not just the two instances of it.

    A function that appends both shapes to one list and passes it to a named-bind
    `execute()` is this defect. It is invisible until the rarer branch is taken,
    and here the rarer branch was the common one.
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for path in sorted((repo / "app").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            shapes: dict[str, set[str]] = {}
            for call in ast.walk(node):
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "append"
                        and isinstance(call.func.value, ast.Name)
                        and call.args):
                    continue
                arg = call.args[0]
                kind = ("dict" if isinstance(arg, ast.Dict)
                        else "tuple" if isinstance(arg, ast.Tuple) else None)
                if kind:
                    shapes.setdefault(call.func.value.id, set()).add(kind)
            for name, kinds in shapes.items():
                if kinds == {"dict", "tuple"}:
                    rel = path.relative_to(repo).as_posix()
                    offenders.append(f"{rel}:{node.lineno} {node.name}() -> {name}")

    assert not offenders, (
        "these build one list from both dicts and tuples; if it reaches a "
        "named-bind execute() it raises on whichever branch is rarer:\n  "
        + "\n  ".join(offenders))
