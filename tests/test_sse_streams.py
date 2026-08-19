"""Server-sent events must still stream now that they come from a router.

W6's first router was chosen to be `prices` precisely because it owns the three
SSE endpoints, and `StreamingResponse` returned from an `APIRouter` is the one
thing in the split that could behave differently from a plain `@app.get`.

The pre-existing hub test only covers the 404 branch, which returns a
`JSONResponse` — it stays green whether or not anything streams. These drive
the streaming branch with a stub generator: no ESI, no price refresh, just the
plumbing.
"""
from __future__ import annotations

import pytest

SSE_BODY = "data: one\n\ndata: two\n\n"


async def _fake_hub_stream(conn, all_ids, region_id):
    yield "data: one\n\n"
    yield "data: two\n\n"


async def _fake_jita_stream(conn, all_ids):
    yield "data: one\n\n"
    yield "data: two\n\n"


def test_hub_refresh_streams_from_the_router(client, monkeypatch):
    from app.web.routers import prices as prices_router

    monkeypatch.setattr(prices_router, "stream_hub_refresh", _fake_hub_stream)
    monkeypatch.setattr(prices_router, "_refresh_type_ids", lambda conn: [34])

    region = next(iter(prices_router.TRADE_HUBS))
    r = client.get(f"/prices/refresh/hub/{region}/stream")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text == SSE_BODY, repr(r.text)


def _stub_station_fetch(monkeypatch):
    """Drive the station stream without ESI. Its generator is inline rather
    than a patchable module-level function, so the stubs go one level down."""
    from app.web.routers import prices as prices_router

    async def _region(conn, location_id, token):
        return 10000002

    async def _volumes(conn, location_id, region_id, type_ids, progress_cb=None):
        if progress_cb:
            progress_cb(len(type_ids), len(type_ids))

    monkeypatch.setattr(prices_router, "get_region_for_location", _region)
    monkeypatch.setattr(prices_router, "fetch_station_volumes", _volumes)
    monkeypatch.setattr(prices_router, "_refresh_type_ids", lambda conn: [34])


def test_the_station_stream_streams_too(client, monkeypatch):
    """An NPC station id (< 1e9) takes the no-token branch, so this exercises
    the third SSE endpoint without a structure or a sign-in."""
    _stub_station_fetch(monkeypatch)

    r = client.get("/prices/refresh/station/stream?location_id=60003760")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert '"done": true' in r.text.lower(), repr(r.text[:400])


@pytest.mark.parametrize("which", ["hub", "jita", "station"])
def test_every_stream_keeps_the_headers_that_stop_proxies_buffering(
    client, monkeypatch, which
):
    """Without these an SSE response arrives in one lump at the end, which
    looks exactly like the progress bar being broken.

    Parametrized over both streams on purpose: the first version of this test
    only checked the hub route, and deleting the headers from the Jita one left
    it green — two near-identical handlers, one of them unpinned.
    """
    from app.web.routers import prices as prices_router

    monkeypatch.setattr(prices_router, "_refresh_type_ids", lambda conn: [34])
    if which == "hub":
        monkeypatch.setattr(prices_router, "stream_hub_refresh", _fake_hub_stream)
        region = next(iter(prices_router.TRADE_HUBS))
        url = f"/prices/refresh/hub/{region}/stream"
    elif which == "jita":
        monkeypatch.setattr(prices_router, "stream_jita_refresh", _fake_jita_stream)
        url = "/prices/refresh/stream"
    else:
        _stub_station_fetch(monkeypatch)
        url = "/prices/refresh/station/stream?location_id=60003760"

    r = client.get(url)

    assert r.headers.get("cache-control") == "no-cache"
    assert r.headers.get("x-accel-buffering") == "no"


def test_the_jita_refresh_stream_also_streams(client, monkeypatch):
    """The other two SSE routes share the shape; this one takes no path
    parameter, so it exercises the plain case."""
    from app.web.routers import prices as prices_router

    monkeypatch.setattr(prices_router, "stream_jita_refresh", _fake_jita_stream)
    monkeypatch.setattr(prices_router, "_refresh_type_ids", lambda conn: [34])

    r = client.get("/prices/refresh/stream")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text == SSE_BODY, repr(r.text)
