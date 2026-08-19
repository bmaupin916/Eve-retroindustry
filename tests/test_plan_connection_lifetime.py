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
