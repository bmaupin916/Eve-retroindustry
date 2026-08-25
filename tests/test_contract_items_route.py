"""`/api/contracts/items` — the endpoint that expands a contract row.

**This file exists because of a regression that shipped.** v0.9.62 moved
`api_contract_items` from `get_conn()` (a `sqlite3` handle) to `connect()` (a
SQLAlchemy `Connection`), and left one raw statement behind it:

    ph = ",".join("?" * len(tids))
    conn.execute(f"... WHERE type_id IN ({ph})", list(tids))

A SQLAlchemy connection rejects that with `ArgumentError: List argument must
consist only of dictionaries`, so the endpoint raised for **every contract that
actually had items** — the normal case. Nothing failed, because the test net
written for that conversion covered `app/character/contracts.py` and this code
is in `app/web/routers/contracts.py`. The module was well tested; the handler
calling it had no test at all.

The general lesson, which is why this is a file and not a line: **converting a
module is not the risky part — converting its callers is.** A caller can hold
statements of its own that the module's tests never see, and the two halves of
`conn.execute(sql, params)` fail differently. A wrong *query* usually raises
somewhere a test is looking. A wrong *parameter style* raises only when the
branch that binds parameters is entered, which here needed a contract that had
items rather than one that merely existed.

So the assertion that matters is the one with a non-empty `tids`.
"""
from __future__ import annotations

import json
import time

import pytest

from app.character import contracts as contracts_api

CONTRACT_ID = 987_654_321
TRITANIUM = 34
PYERITE = 35


@pytest.fixture
def cached_items(app_module):
    """A contract whose items carry type_ids, so the name lookup actually runs.

    Seeded through the module's own writer rather than by hand: if the storage
    shape changes, this moves with it instead of silently testing a format
    nothing writes any more.
    """
    conn = app_module.get_conn()
    try:
        conn.execute("DELETE FROM contract_items_cache WHERE contract_id=?",
                     (CONTRACT_ID,))
        conn.execute(
            "INSERT INTO contract_items_cache (contract_id, data_json, cached_at)"
            " VALUES (?,?,?)",
            (CONTRACT_ID,
             json.dumps([{"type_id": TRITANIUM, "quantity": 1000, "is_included": True},
                         {"type_id": PYERITE, "quantity": 500, "is_included": False}]),
             time.time()))
        conn.commit()
    finally:
        conn.close()
    yield
    conn = app_module.get_conn()
    try:
        conn.execute("DELETE FROM contract_items_cache WHERE contract_id=?",
                     (CONTRACT_ID,))
        conn.commit()
    finally:
        conn.close()


def test_expanding_a_contract_with_items_returns_them(client, cached_items):
    """The regression test. Before the fix this was a 500 from
    `ArgumentError`, because `tids` was non-empty and the name lookup ran."""
    r = client.get(f"/api/contracts/items?contract_id={CONTRACT_ID}")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["type_id"] for i in items] == [TRITANIUM, PYERITE]


def test_the_item_names_are_resolved_from_the_sde(client, cached_items):
    """Not `#34`. The fallback is a `#id` string, so a lookup that returns
    nothing still renders — which is exactly how a broken query stays quiet."""
    r = client.get(f"/api/contracts/items?contract_id={CONTRACT_ID}")

    names = {i["type_id"]: i["name"] for i in r.json()["items"]}
    assert names[TRITANIUM] == "Tritanium"
    assert not names[TRITANIUM].startswith("#"), (
        "the SDE lookup returned nothing and the fallback covered for it")


def test_quantity_and_inclusion_survive(client, cached_items):
    r = client.get(f"/api/contracts/items?contract_id={CONTRACT_ID}")

    items = {i["type_id"]: i for i in r.json()["items"]}
    assert items[TRITANIUM]["quantity"] == 1000
    assert items[TRITANIUM]["included"] is True
    assert items[PYERITE]["included"] is False, (
        "is_included was dropped — a courier's excluded items would read as "
        "part of the cargo")


def test_an_unexpanded_contract_returns_an_empty_list(client):
    """No cache row, no character or corporation id to fetch with: the handler
    must answer rather than raise. This is the path that *did* pass while the
    populated one raised, because `tids` is empty and the lookup never runs."""
    r = client.get("/api/contracts/items?contract_id=1")

    assert r.status_code == 200
    assert r.json()["items"] == []
