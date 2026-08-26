"""The SDE connection `/plan` opens must not outlive the request that opened it.

`plan_result` opens `connect_to_path(database_path())` for the converted
`BOMResolver` and used to close it with a bare `sde_conn.close()` forty lines
further down, with no `try`/`finally` in between. Every call in that span can
raise — `BOMResolver.resolve`, `build_invention_params`, `_derive_job_splits` —
and each of those paths skipped the close.

Nothing surfaced it. The handler catches `Exception` and renders the message
into the page's error banner, so a leaking request is a **200** with a polite
red box; and `connect_to_path` builds its engine with `NullPool`, so every call
is a fresh sqlite3 handle rather than one returned to a pool. A user who
repeatedly planned something unbuildable leaked a file handle per attempt,
held until the process exited.
"""
from __future__ import annotations


def _post_plan(client):
    """A plan request that reaches the resolver span."""
    return client.post("/plan", data={"product": "Wasp II", "qty": "10",
                                      "station": "60003760", "mode": "full",
                                      "runs_per_job": "0", "form_me": "0"})


def _spy_on_connect_to_path(monkeypatch):
    """Record every SDE connection the handler opens.

    The router does `from app.db.conn import ... connect_to_path` *inside*
    `plan_result`, so the import runs per request and patching the source
    module is enough — the router's own namespace never caches it.
    """
    import app.db.conn as conn_mod

    opened = []
    real = conn_mod.connect_to_path

    def spy(db_path):
        conn = real(db_path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(conn_mod, "connect_to_path", spy)
    return opened


def test_a_raising_resolve_still_closes_the_sde_connection(client, monkeypatch):
    """The regression proper: resolve raises, the connection still closes."""
    from app.web.routers import plan as plan_router

    opened = _spy_on_connect_to_path(monkeypatch)

    class ExplodingResolver(plan_router.BOMResolver):
        def resolve(self, *args, **kwargs):
            raise RuntimeError("resolve blew up")

    monkeypatch.setattr(plan_router, "BOMResolver", ExplodingResolver)

    response = _post_plan(client)

    # 200 even though the resolve failed — that is the handler's error banner,
    # and it is exactly why the leak went unnoticed for so long.
    assert response.status_code == 200
    assert opened, (
        "the handler never opened an SDE connection, so this test is not "
        "exercising the span it exists to guard — check the form payload"
    )
    assert all(conn.closed for conn in opened), (
        "an SDE connection outlived a /plan request whose resolve raised: "
        f"{sum(not c.closed for c in opened)} of {len(opened)} left open"
    )


def test_the_happy_path_closes_the_sde_connection_too(client, monkeypatch):
    """The `finally` half is worthless if the normal path regressed instead.

    Without this, moving the close into an exception handler rather than a
    `with`/`finally` would still satisfy the test above.
    """
    opened = _spy_on_connect_to_path(monkeypatch)

    response = _post_plan(client)

    assert response.status_code == 200
    assert opened, "the handler never opened an SDE connection"
    assert all(conn.closed for conn in opened), (
        "an SDE connection outlived a successful /plan request"
    )


# ── the same leak, one layer down ────────────────────────────────────────────

def test_build_plan_closes_its_connection_when_something_raises(tmp_path):
    """`app/manufacturing/planner.py::build_plan` had the identical shape twice.

    It used to open **two** connections — `connect_to_path(db_path)` for the
    resolver and a raw `sqlite3.connect(db_path)` for the blueprint lookup — and
    close both with bare calls at the bottom of a span containing
    `find_blueprint_for_product` and `resolver.resolve`. `plan_result` calls it
    from inside the same `except Exception` handler, so a failure here leaked
    **two** handles per attempt with no symptom at all.

    **There is one connection now.** `find_blueprint_for_product` moved onto the
    portable query layer in v0.9.73, so it takes the resolver's connection and
    the raw one is gone entirely. This test was renamed rather than deleted: the
    leak it guards is a property of the `try/finally`, not of how many handles
    are inside it.

    Worth keeping for the trap it records even though no `sqlite3` handle
    survives here to fall into it: a `sqlite3.Connection` used as a bare context
    manager commits or rolls back its transaction and does **not** close the
    connection. Only `contextlib.closing` closes it. A "fixed" version using
    `with` looks right, passes any test that checks for an exception, and leaks
    exactly as before. That is why the old code wrapped it the way it did, and
    the reasoning outlives the code.

    The `opened_raw` half of this test is kept as a **negative** assertion for
    the same reason the positive controls exist elsewhere: if a raw
    `sqlite3.connect` ever reappears inside `build_plan`, that is a regression
    worth failing on rather than silently re-permitting.
    """
    import shutil
    import sqlite3

    import app.db.conn as conn_mod
    from app.manufacturing import planner

    db = str(tmp_path / "plan.db")
    shutil.copy2("sde_base.db", db)

    opened_engine = []
    real_ctp = conn_mod.connect_to_path

    def spy_ctp(path):
        c = real_ctp(path)
        opened_engine.append(c)
        return c

    opened_raw = []
    real_connect = sqlite3.connect

    def spy_connect(*a, **kw):
        c = real_connect(*a, **kw)
        opened_raw.append(c)
        return c

    conn_mod.connect_to_path = spy_ctp
    sqlite3.connect = spy_connect
    try:
        # A product id the SDE has no blueprint for makes `resolve` raise.
        try:
            planner.build_plan(product_type_id=1, quantity=1,
                               location_id=60003760, available_assets={},
                               blueprints=[], db_path=db, mode="full")
        except Exception:
            pass
    finally:
        conn_mod.connect_to_path = real_ctp
        sqlite3.connect = real_connect

    assert opened_engine, "the spy never saw the engine connection — retarget this"

    for c in opened_engine:
        assert c.closed, "the engine connection outlived a failed build_plan"

    # Now a regression guard rather than a leak check. `build_plan` opened a raw
    # sqlite3 handle until v0.9.73 and no longer does; if one comes back, the
    # `contextlib.closing` trap in the docstring comes back with it.
    assert not opened_raw, (
        f"build_plan opened {len(opened_raw)} raw sqlite3 connection(s) — it "
        f"should use the engine connection it already has")
