"""No page may render by waiting on ESI.

Step 4 of `docs/design-hosted-v2.md`. A page that fetches while rendering has
three problems, and only the first is obvious:

* its load time is the sum of N round trips to CCP, on somebody else's network;
* each render spends the shared error budget, so *looking* at a page can
  error-limit the sync that keeps it useful;
* it fails when ESI does, for a page whose data has not changed in an hour.

The background worker in `app/sync/worker.py` exists to make the caches good
enough that no page needs to. This scan is what keeps it that way — a handler
that starts fetching again is the regression, and it does not announce itself.

**Not everything is a page.** Some routes exist precisely to fetch: an image
proxy has to go and get the image once, and a button labelled "Load" is the
user asking for a round trip. Those are listed below by name, with a reason
each, rather than pattern-matched — a rule that exempts anything ending in
`_refresh` is a rule somebody will name a page after.

**What this cannot see.** The scan reads the router modules, following calls
into helpers defined in the same file. It does not cross modules, so a handler
whose fetch happens inside `prices_helper` or `contracts_helper` looks clean
here — `POST /prices/refresh` is the plain example: it calls
`refresh_jita_prices_all`, which certainly fetches, and this scan says nothing
about it. That is a real hole, not a detail: a page converted by moving its
fetch into a helper would pass. The `ALLOWED` list therefore names only what
the scan actually flags, and `test_the_exemption_list_has_no_dead_entries`
keeps it that way — an entry the scan never consults is a claim nobody checks.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROUTERS = REPO / "app" / "web" / "routers"

#: Handler -> why it may reach ESI while serving a request.
ALLOWED = {
    # Explicitly user-triggered fetches: a button, not a page load.
    "prices_refresh_stream": "the Refresh button, streamed",
    "prices_refresh_hub_stream": "the Refresh button, per hub",
    "prices_station_stream": "the Load button on a custom station",
    "api_station_volume": "the Load button, non-streamed path",
    "fetch_plan_sell_price": "the user asking this item's price at this station",
    "api_market_orders": "the user opening one item's order book",
    "api_public_index": "the user indexing a region, streamed with progress",

    # Answer *is* the fetch: nothing local could serve these.
    "suggest_station": "resolving a name the user just typed",
    "add_station": "resolving a station the user just pasted",
    "location_resolve": "resolving one id on demand",
    "my_location": "where the character is right now, which is the question",
    "assets_distances": "jump counts from the character's current position",

    # Image proxies with their own disk cache; the first request has to fetch.
    "entity_portrait": "image proxy, cached to disk after the first fetch",
    "type_icon": "image proxy, cached to disk after the first fetch",

    # The sync itself. Fetching is the whole request.
    "auth_sync": "the post-login sync, which is what the page is",
    "api_sync_start": "the Sync All button",

    # Does not block the render: the result is a task nobody awaits, so the
    # page returns while it runs. Warming the cache for the next search.
    "prices_search": "fire-and-forget cache warm-up, not awaited",

    # One lookup per process, memoised in _REGIONS_CACHE. Regions do not change.
    "public_contracts_page": "the region list, fetched once per process",

    # Not yet converted. Each needs a cache the worker fills first; /jobs is
    # the worked example. Remove a name from here when its page stops fetching,
    # never to make this test pass.
    "planets_page": "TODO: no cache for colony detail yet",
    "pi_planner_page": "TODO: shares the colony fetch with /planets",
    "api_pi_alerts": "TODO: refreshes live when the cache is stale",
    "assets_page": "TODO: cache-aware fetchers, but still fetches when stale",
    "blueprints_page": "TODO: cache-aware fetchers, but still fetches when stale",
    "contracts_page": "TODO: no cache at all yet",
    "api_contract_items": "TODO: no cache at all yet",
    "plan_form": "TODO: cache-aware fetchers, but still fetches when stale",
    "plan_result": "TODO: cache-aware fetchers, but still fetches when stale",
}


def _called_names(node) -> set[str]:
    """Every name called anywhere inside `node`, nested functions included."""
    return {
        (getattr(c.func, "id", None) or getattr(c.func, "attr", None) or "")
        for c in ast.walk(node) if isinstance(c, ast.Call)
    }


def _reaches_esi(name: str) -> bool:
    return name == "esi_client" or name.startswith("fetch_") or name.startswith("stream_")


def _fetching_handlers() -> dict[str, set[str]]:
    """Route handlers that reach ESI while serving a request.

    Follows calls into module-level helpers in the same router, because
    `contracts_page` fetching directly and `contracts_page` calling
    `_finalize_contracts` which fetches are the same thing to whoever is
    waiting for the page.

    **It does not cross modules.** A handler whose fetch happens inside
    `prices_helper` is invisible here — which is why the exemption list below
    does not name those handlers, and why removing a fetch from a router is not
    the same as proving the page no longer waits on ESI. The dead-entry test is
    what stops that limitation being quietly used as a loophole.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local = {n.name: n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_route = any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and getattr(d.func.value, "id", "") == "router"
                for d in node.decorator_list
            )
            if not is_route:
                continue

            calls, seen, queue = set(), {node.name}, [node]
            while queue:
                current = queue.pop()
                for name in _called_names(current):
                    if _reaches_esi(name):
                        calls.add(name)
                    elif name in local and name not in seen:
                        seen.add(name)
                        queue.append(local[name])
            if calls:
                found[node.name] = calls
    return found


def test_the_scan_finds_the_handlers_that_do_fetch():
    """A positive control: the list below is meant to shrink to nothing, and an
    empty result would look like success at every point on the way there."""
    found = _fetching_handlers()
    assert found, "the scan matched no handlers at all — it has stopped working"
    assert "type_icon" in found, "the scan missed a handler that certainly fetches"


def test_no_page_fetches_from_esi_while_rendering():
    offenders = {
        name: sorted(calls) for name, calls in _fetching_handlers().items()
        if name not in ALLOWED
    }
    assert not offenders, (
        "these route handlers reach ESI while serving a request:\n  "
        + "\n  ".join(f"{n}: {c}" for n, c in sorted(offenders.items()))
        + "\nGive it a cache the background worker fills, and read that instead "
          "— app/web/routers/industry.py::jobs_page is the worked example."
    )


def test_the_exemption_list_has_no_dead_entries():
    """An exemption for a handler that no longer fetches is a licence nobody
    revoked. It is also how this list would quietly stop describing the app."""
    fetching = set(_fetching_handlers())
    stale = sorted(set(ALLOWED) - fetching)
    assert not stale, (
        "these are exempted but no longer fetch — delete them from ALLOWED:\n  "
        + "\n  ".join(stale)
    )


def test_the_jobs_page_is_cache_only():
    """The first one converted, asserted by name so the example cannot rot."""
    src = (ROUTERS / "industry.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    handler = next(n for n in tree.body
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "jobs_page")

    calls = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
             for c in ast.walk(handler) if isinstance(c, ast.Call)}
    assert "esi_client" not in calls
    assert not any((c or "").startswith("fetch_") for c in calls)
    assert "load_cached_jobs" in calls, "it no longer reads the cache either"


def test_the_jobs_page_says_how_old_its_answer_is(client, app_module):
    """A cache-only page with no age on it is indistinguishable from a stale
    one, which is the fair complaint about rendering from cache."""
    import json
    import time

    conn = app_module.get_conn()
    try:
        conn.execute("DELETE FROM char_jobs_cache")
        conn.execute(
            "INSERT INTO char_jobs_cache (character_id, data_json, cached_at)"
            " VALUES (?,?,?)",
            (900000001, json.dumps([]), time.time() - 3600))
        conn.commit()
    finally:
        conn.close()

    page = client.get("/jobs").text
    assert "As of" in page, "the page does not say when it was last synced"
    assert "never waits on ESI" in page


def test_the_jobs_page_tells_an_unsynced_character_from_an_idle_one(client, app_module):
    """"No jobs" and "not looked yet" are different answers, and only one of
    them is true before the first sync."""
    conn = app_module.get_conn()
    try:
        conn.execute("DELETE FROM char_jobs_cache")
        conn.commit()
    finally:
        conn.close()

    page = client.get("/jobs").text
    assert "not been synced yet" in page, (
        "a character the worker has not reached was reported as having no jobs"
    )
