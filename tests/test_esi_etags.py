"""Conditional requests: ask ESI what changed, not what it holds.

ESI answers every GET with an ETag and honours If-None-Match. A 304 costs one
token of the error budget instead of two, carries no body, and comes back in a
fraction of the time — and most of what this app fetches (assets, blueprints,
skills, contracts) changes far less often than it is asked for.

It lives in the transport so no caller has to learn about 304: a hit is replayed
as the 200 it was. That replay is the part worth testing hardest, because a
caller that reads `X-Pages` off a stripped response silently stops paginating,
which looks like a character owning fewer assets rather than like a bug.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.esi import client as esi


@pytest.fixture(autouse=True)
def clean_caches():
    esi._ETAGS.reset()
    esi._QUARANTINE.reset()
    yield
    esi._ETAGS.reset()
    esi._QUARANTINE.reset()


class _ScriptedTransport(esi._GovernedTransport):
    """The real governed transport with a scripted server behind it.

    The server behaves like ESI: it holds one body and one ETag, answers 304
    when If-None-Match matches, and records what it was asked.
    """

    def __init__(self, body=b'{"v":1}', etag='"abc"', extra_headers=None):
        super().__init__()
        self.body = body
        self.etag = etag
        self.extra_headers = extra_headers or {}
        self.requests: list[dict] = []

    async def _serve(self, request):
        self.requests.append({
            "url": str(request.url),
            "if_none_match": request.headers.get("if-none-match"),
        })
        headers = {"ETag": self.etag, "Content-Type": "application/json",
                   **self.extra_headers}
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304, request=request, headers=headers)
        return httpx.Response(200, request=request, content=self.body, headers=headers)


@pytest.fixture
def server(monkeypatch):
    def _make(**kw):
        t = _ScriptedTransport(**kw)

        async def _fake(request):
            return await t._serve(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                            lambda self, request: _fake(request))
        return t
    return _make


URL = "https://esi.evetech.net/latest/characters/95123456/assets/"


def _get(t, url=URL, headers=None):
    async def run():
        return await t.handle_async_request(
            httpx.Request("GET", url, headers=headers or {}))
    return asyncio.run(run())


def test_the_second_request_asks_conditionally(server):
    t = server()

    first = _get(t)
    assert first.status_code == 200
    assert t.requests[0]["if_none_match"] is None, "nothing was known yet"

    _get(t)
    assert t.requests[1]["if_none_match"] == '"abc"', (
        "the second request did not carry If-None-Match, so ESI sent the whole "
        "body again and it cost two tokens instead of one"
    )


def test_a_304_comes_back_to_the_caller_as_the_200_it_was(server):
    """No call site knows about 304, and none should have to."""
    t = server(body=b'{"items":[1,2,3]}')

    _get(t)
    again = _get(t)

    assert again.status_code == 200, "the caller was handed a bare 304"
    assert again.json() == {"items": [1, 2, 3]}
    assert again.headers.get("x-eve-retroindustry-etag") == "hit"


def test_the_replay_keeps_the_pagination_header(server):
    """`X-Pages` decides how many more requests the paginated fetchers make.

    Losing it on a cache hit does not fail — it silently returns page one and
    calls that the whole answer, which reads as a character owning fewer assets.
    """
    t = server(extra_headers={"X-Pages": "7"})

    assert _get(t).headers["x-pages"] == "7"
    assert _get(t).headers.get("x-pages") == "7", (
        "the cached replay dropped X-Pages, so the caller would stop after one page"
    )


def test_a_changed_body_replaces_the_cached_one(server):
    t = server(body=b'{"v":1}', etag='"one"')
    assert _get(t).json() == {"v": 1}

    t.body, t.etag = b'{"v":2}', '"two"'
    fresh = _get(t)
    assert fresh.json() == {"v": 2}, "a stale body was served after ESI changed it"
    assert fresh.headers.get("x-eve-retroindustry-etag") is None

    # And the new ETag is what gets offered next time.
    _get(t)
    assert t.requests[-1]["if_none_match"] == '"two"'


def test_two_characters_do_not_share_an_entry(server):
    t = server()
    other = "https://esi.evetech.net/latest/characters/95999999/assets/"

    _get(t)
    _get(t, other)
    assert t.requests[1]["if_none_match"] is None, (
        "a different character's URL reused the first one's ETag"
    )


def test_the_same_url_with_a_different_token_does_not_share_an_entry(server):
    """Corp endpoints answer differently per member — same URL, different roles."""
    t = server()
    url = "https://esi.evetech.net/latest/corporations/98000001/assets/"

    _get(t, url, headers={"Authorization": "Bearer aaa"})
    _get(t, url, headers={"Authorization": "Bearer bbb"})

    assert t.requests[1]["if_none_match"] is None, (
        "one member's cached response was offered for another member's request"
    )


def test_the_stored_key_does_not_contain_the_token(server):
    t = server()
    _get(t, headers={"Authorization": "Bearer super-secret-token"})
    assert not any("super-secret-token" in k for k in esi._ETAGS._entries), (
        "the bearer token was stored in the cache key"
    )


def test_a_caller_that_sets_its_own_if_none_match_is_left_alone(server):
    """`app/market/prices.py` runs its own persisted ETag store, because market
    history has to be recomputed against a moving window rather than replayed."""
    t = server()
    _get(t)                                          # populate
    _get(t, headers={"If-None-Match": '"caller-supplied"'})
    assert t.requests[1]["if_none_match"] == '"caller-supplied"', (
        "the transport overwrote a caller's own conditional header"
    )


def test_nothing_is_cached_for_a_failure(server):
    t = server()

    async def _fail(request):
        t.requests.append({"url": str(request.url),
                           "if_none_match": request.headers.get("if-none-match")})
        return httpx.Response(500, request=request, headers={"ETag": '"x"'})

    _get(t)                                          # a good 200 first
    assert esi.etag_stats()["entries"] == 1

    import httpx as _httpx
    original = _httpx.AsyncHTTPTransport.handle_async_request
    _httpx.AsyncHTTPTransport.handle_async_request = lambda self, request: _fail(request)
    try:
        assert _get(t).status_code == 500
    finally:
        _httpx.AsyncHTTPTransport.handle_async_request = original

    assert esi.etag_stats()["entries"] == 0, (
        "a failed response left the old body cached, so the next hit would "
        "serve data ESI never confirmed"
    )


def test_the_cache_stays_within_its_budget(server):
    """A big account is a few MB per fetch; the cap keeps a long-running
    process from holding everything it ever asked for."""
    esi._ETAGS.reset()
    big = b"x" * (esi._ETagCache.MAX_ENTRY + 1)
    t = server(body=big)
    _get(t)
    assert esi.etag_stats()["entries"] == 0, "an oversized body was cached anyway"

    esi._ETAGS.reset()
    t2 = server(body=b"y" * 1024, etag='"small"')
    _get(t2)
    assert esi.etag_stats()["entries"] == 1
    assert esi.etag_stats()["bytes"] == 1024


def test_eviction_drops_the_oldest_first():
    """Unit-level, because filling 32 MB through the transport is slow."""
    cache = esi._ETagCache()
    cache.MAX_BYTES = 3000

    def _store(name, size):
        request = httpx.Request("GET", f"https://esi.evetech.net/latest/{name}/")
        response = httpx.Response(200, request=request, headers={"ETag": f'"{name}"'})
        cache.store(cache.key(request), response, b"z" * size)

    _store("a", 1000)
    _store("b", 1000)
    _store("c", 1000)
    _store("d", 1000)          # pushes past the cap

    keys = " ".join(cache._entries)
    assert "/a/" not in keys, "eviction kept the oldest entry"
    assert "/d/" in keys, "eviction dropped the newest entry"
    assert cache.stats()["bytes"] <= cache.MAX_BYTES
