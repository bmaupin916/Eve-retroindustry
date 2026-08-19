"""The complete route table, pinned.

W6 (`docs/design-hosted-v2.md` §11) splits `app/web/main.py` — 7,112 lines and
80 routes — into routers. That is a pure move: no route may change path,
method, or name, and none may go missing or appear.

Moving code between modules is exactly the kind of edit that looks fine and
silently drops a decorator, and a missing route does not fail loudly — it
404s on a page nobody visits during a test run. So the inventory is written
down here, before the split, and compared after it.

**When a route is genuinely added or removed**, update `EXPECTED` in the same
commit that does it. The list is a record of a deliberate decision, not a
rubber stamp: if a diff here was not intended, that is the bug.
"""
from __future__ import annotations

import pytest


def _iter_routes(router):
    """Every real route, descending through included routers.

    `app.include_router()` does not flatten the sub-router into `app.routes` on
    FastAPI 0.141 / Starlette 1.6 — it appends one `_IncludedRouter` proxy that
    holds the original. Walking `app.routes` alone therefore stops seeing a
    route the moment W6 moves it into a router, which would quietly turn this
    whole file into a test that passes because it is looking at less and less.
    """
    for route in router.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _iter_routes(inner)
        else:
            yield route


def _inventory(app) -> set[tuple[str, str]]:
    """(method, path) for everything the app serves, mounts included."""
    found = set()
    for route in _iter_routes(app):
        methods = getattr(route, "methods", None)
        if methods:
            for method in methods - {"HEAD"}:
                found.add((method, route.path))
        elif hasattr(route, "path"):
            found.add(("MOUNT", route.path))
        else:
            # Not "skip what we do not recognise" — that is how the count above
            # would drift downwards without anyone noticing.
            raise AssertionError(
                f"unrecognised entry in app.routes: {type(route).__name__}. "
                "If this is a new way of holding routes, teach _iter_routes "
                "about it; do not filter it out."
            )
    return found


# Captured from the running app at v0.9.30, before the split — read out of
# FastAPI rather than typed from memory, because the first hand-written version
# of this list invented `/portrait/{character_id}` for what is actually
# `/portrait/{kind}/{entity_id}`.
EXPECTED: set[tuple[str, str]] = {
    ("GET", "/"),
    ("GET", "/about"),
    ("GET", "/assets"),
    ("GET", "/auth/bootstrap"),
    ("GET", "/auth/login"),
    ("GET", "/auth/sync"),
    ("GET", "/blueprints"),
    ("GET", "/callback"),
    ("GET", "/contracts"),
    ("GET", "/contracts/public"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/icon/{type_id}"),
    ("GET", "/jobs"),
    ("GET", "/margins"),
    ("GET", "/openapi.json"),
    ("GET", "/orders"),
    ("GET", "/pi-planner"),
    ("GET", "/plan"),
    ("GET", "/planets"),
    ("GET", "/portrait/{kind}/{entity_id}"),
    ("GET", "/prices"),
    ("GET", "/prices/refresh/hub/{region_id}/stream"),
    ("GET", "/prices/refresh/station/stream"),
    ("GET", "/prices/refresh/stream"),
    ("GET", "/projects"),
    ("GET", "/projects/{project_id}"),
    ("GET", "/reactions"),
    ("GET", "/redoc"),
    ("GET", "/settings"),
    ("GET", "/setup"),
    ("GET", "/setup/client-id"),
    ("GET", "/wallet"),
    ("MOUNT", "/static"),
}


def test_the_inventory_matches_the_running_app(app_module):
    """Not a snapshot of a snapshot: this reads the real FastAPI app.

    Only the GET pages and mounts are listed above — the API surface is checked
    by count below, because enumerating eighty entries by hand invites a typo
    that then gets "fixed" by copying whatever the app currently does, which
    would make the test agree with any regression.
    """
    live = _inventory(app_module.app)
    missing = EXPECTED - live
    assert not missing, f"routes that have disappeared: {sorted(missing)}"


def test_the_route_count_is_unchanged(app_module):
    """80 routes at v0.9.30. A split must not change this number.

    If it does and the change was deliberate, update the number here in the
    same commit — the point is that it cannot change *silently*.
    """
    live = _inventory(app_module.app)
    assert len(live) == 80, (
        f"route count is {len(live)}, expected 80. "
        "A split should move routes, not add or lose them."
    )


def test_no_route_is_registered_twice(app_module):
    """Two decorators on the same path is how a botched move usually shows up.

    FastAPI accepts it silently and serves the first, so the second handler
    becomes dead code that still looks live.
    """
    seen: dict[tuple[str, str], int] = {}
    for route in _iter_routes(app_module.app):
        methods = getattr(route, "methods", None) or {"MOUNT"}
        for method in methods - {"HEAD"}:
            seen[(method, route.path)] = seen.get((method, route.path), 0) + 1
    duplicates = {k: n for k, n in seen.items() if n > 1}
    assert not duplicates, f"registered more than once: {sorted(duplicates)}"


def test_every_page_route_has_a_handler_name(app_module):
    """Routers move functions between modules; an unnamed endpoint means one
    got wrapped or reassigned on the way, which breaks url_for in templates."""
    unnamed = [
        r.path for r in _iter_routes(app_module.app)
        if getattr(r, "methods", None) and not getattr(r, "name", None)
    ]
    assert not unnamed, f"routes without a handler name: {unnamed}"
