"""One revoked token must not error-limit everybody.

ESI keeps a single error budget for the whole client, in a ~60 second sliding
window, and a 4xx costs 5 tokens out of it. A character who removed the app
in-game answers 401 or 403 to every authenticated call, forever — so a sync that
touches that character once a minute quietly spends the budget that the rest of
the app, and every other character, needs.

The error-limit governor in `app/esi/client.py` can only react once the budget
is nearly gone. The quarantine stops the spending: after three consecutive
refusals for one entity, its requests are answered locally and never reach the
wire.

These tests drive the transport directly rather than through a route, because
what is being asserted is *that no request was made* — which no route can show.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.esi import client as esi


@pytest.fixture(autouse=True)
def clean_quarantine():
    esi._QUARANTINE.reset()
    yield
    esi._QUARANTINE.reset()


class _CountingTransport(esi._GovernedTransport):
    """The real governed transport with the network replaced by a script."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls: list[str] = []

    async def _real_send(self, request):
        self.calls.append(str(request.url))
        status = self._responses.pop(0) if self._responses else 200
        return httpx.Response(status, request=request, json={})


@pytest.fixture
def transport(monkeypatch):
    """Patch the base-class send so nothing leaves the machine."""
    def _make(responses):
        t = _CountingTransport(responses)

        async def _fake_super(request):
            return await t._real_send(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                            lambda self, request: _fake_super(request))
        return t
    return _make


def _get(t, url):
    async def run():
        request = httpx.Request("GET", url)
        return await t.handle_async_request(request)
    return asyncio.run(run())


CHAR = "https://esi.evetech.net/latest/characters/95123456/assets/"
OTHER = "https://esi.evetech.net/latest/characters/95999999/assets/"
CORP = "https://esi.evetech.net/latest/corporations/98000001/assets/"
PUBLIC = "https://esi.evetech.net/latest/markets/10000002/orders/"


def test_the_key_names_the_entity_not_the_endpoint():
    k = esi._EntityQuarantine.key
    assert k(httpx.URL(CHAR)) == "characters/95123456"
    assert k(httpx.URL(CORP)) == "corporations/98000001"
    assert k(httpx.URL("https://esi.evetech.net/latest/characters/1/wallet/journal/")) \
        == "characters/1"
    # Public routes carry no entity, so a 403 there quarantines nothing.
    assert k(httpx.URL(PUBLIC)) is None
    assert k(httpx.URL("https://esi.evetech.net/latest/universe/names/")) is None


def test_a_broken_character_stops_reaching_the_network(transport):
    t = transport([403] * 10)

    for _ in range(esi._EntityQuarantine.STRIKES):
        assert _get(t, CHAR).status_code == 403
    made = len(t.calls)
    assert made == esi._EntityQuarantine.STRIKES

    # Everything after this is answered locally.
    for _ in range(5):
        r = _get(t, CHAR)
        assert r.status_code == 403
        assert r.headers.get("X-Eve-Retroindustry-Quarantined") == "characters/95123456"
    assert len(t.calls) == made, (
        f"{len(t.calls) - made} requests still went out for a quarantined "
        "character — each one costs 5 tokens of the shared error budget"
    )


def test_quarantining_one_character_leaves_the_others_alone(transport):
    t = transport([403, 403, 403] + [200] * 5)

    for _ in range(esi._EntityQuarantine.STRIKES):
        _get(t, CHAR)
    assert _get(t, CHAR).status_code == 403          # held, no call

    before = len(t.calls)
    assert _get(t, OTHER).status_code == 200
    assert _get(t, CORP).status_code == 200
    assert len(t.calls) == before + 2, (
        "a second character's requests were held back by the first one's token"
    )


def test_a_public_route_is_never_quarantined(transport):
    t = transport([403] * 10)
    for _ in range(6):
        assert _get(t, PUBLIC).status_code == 403
    assert len(t.calls) == 6, "a public 403 held back a route that has no entity"


def test_two_refusals_are_not_enough(transport):
    """A transient 403 — a token mid-rotation, an ESI hiccup — must not park a
    working character for a minute."""
    t = transport([403, 403, 200, 200])

    _get(t, CHAR)
    _get(t, CHAR)
    assert _get(t, CHAR).status_code == 200
    assert len(t.calls) == 3
    assert esi.quarantine_state() == {}

    # And the success reset the count rather than leaving it at two: another
    # pair of refusals must still not be enough. Asserting the state is empty
    # would pass either way, since two strikes never quarantine on their own.
    t._responses = [403, 403]
    _get(t, CHAR)
    _get(t, CHAR)
    assert esi.quarantine_state() == {}, (
        "two refusals after a success quarantined — the success did not clear "
        "the earlier strikes"
    )


def test_a_success_releases_the_entity(transport):
    t = transport([403, 403, 403])
    for _ in range(esi._EntityQuarantine.STRIKES):
        _get(t, CHAR)
    assert "characters/95123456" in esi.quarantine_state()

    # Pretend the window elapsed: the next request is let through to check.
    esi._QUARANTINE._held_until["characters/95123456"] = 0.0
    t._responses = [200]
    assert _get(t, CHAR).status_code == 200
    assert esi.quarantine_state() == {}, "a working token stayed quarantined"

    # The release must be a real reset, not just an expired timer. If the strike
    # count survived, the very next refusal would quarantine again — and the
    # probe that let this request through deliberately leaves the count one
    # short, so this is the difference between clearing and not.
    t._responses = [403, 403]
    _get(t, CHAR)
    _get(t, CHAR)
    assert esi.quarantine_state() == {}, (
        "the successful call did not clear the strikes it had accumulated"
    )


def test_the_backoff_lengthens_each_time(transport):
    t = transport([403] * 30)
    key = "characters/95123456"

    seen = []
    for _ in range(3):
        for _ in range(esi._EntityQuarantine.STRIKES):
            _get(t, CHAR)
        seen.append(esi.quarantine_state()[key])
        esi._QUARANTINE._held_until[key] = 0.0     # elapse the window
        _get(t, CHAR)                              # the one probe it allows

    assert seen == sorted(seen) and len(set(seen)) == 3, (
        f"backoff did not lengthen across repeated failures: {seen}"
    )


def test_a_404_is_an_answer_not_a_broken_token(transport):
    """"This character has no industry jobs" is a 404, and quarantining on it
    would park a perfectly good character."""
    t = transport([404] * 6)
    for _ in range(6):
        assert _get(t, CHAR).status_code == 404
    assert len(t.calls) == 6
    assert esi.quarantine_state() == {}
