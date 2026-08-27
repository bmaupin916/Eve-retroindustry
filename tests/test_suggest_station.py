"""Station suggestions in the Prices custom-station box.

**CCP removed the public `/search/` endpoint.** It answers 404 on every version
for every query, including ones that cannot fail, and that quietly cost this box
two of its four sources: NPC stations by name, and the system-name lookup
everything else keys off. The visible symptom is a nullsec system typed in full
finding nothing even though it has an NPC station — without a system id the code
never reaches "structures in this system" or "NPC stations in this system".

Two things made it invisible rather than loud. A 404 here produces an empty
list, not an error; and the *authenticated* `/characters/{id}/search/` still
exists, so player structures the character can dock at kept turning up and the
box went on looking alive.

`POST /universe/ids/` is the documented replacement, and it matches **whole
names only** — the old `strict=false` partial matching is gone for good. The
local name cache is what still does substring matching, over everything already
known.

The stubs patch `app.web.routers.locations`, not the modules that *define* these
names: the router does `from app.esi.client import esi_client`, which binds the
function object at import, so patching the source module would leave the router
pointing at the original and the guard would pass no matter what the page did.
"""
from __future__ import annotations

import pytest

from app.web.routers import locations as loc

PR_8CA_SYSTEM = 30004711
PR_8CA_STATION = 60014946
PR_8CA_NAME = "PR-8CA III - Blood Raiders Logistic Support"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeClient:
    """Records every ESI call, so a test can assert which endpoint was used."""

    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self.calls.append(("POST", url))
        if "/universe/ids/" in url:
            return _Resp({"systems": [{"id": PR_8CA_SYSTEM, "name": "PR-8CA"}]})
        return _Resp({})

    async def get(self, url, **kw):
        self.calls.append(("GET", url))
        if "/universe/systems/" in url:
            return _Resp({"stations": [PR_8CA_STATION]})
        return _Resp({})


@pytest.fixture
def stub_esi(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(loc, "esi_client", lambda **kw: _FakeClient(calls))

    async def _names(ids, token=None):
        return {sid: PR_8CA_NAME for sid in ids}

    monkeypatch.setattr(loc, "_resolve_names", _names)
    monkeypatch.setattr(loc, "_locations_in_system", lambda sid: [])
    return calls


def test_a_system_name_finds_the_npc_station_in_it(client, stub_esi):
    """The reported failure. PR-8CA has an NPC station, so typing the system
    name has to reach it — via `/universe/ids/` for the system, then
    `/universe/systems/{id}/` for the stations inside it."""
    r = client.get("/api/suggest-station?q=PR-8CA")

    assert r.status_code == 200
    found = {e["location_id"] for e in r.json()["other"]}
    assert PR_8CA_STATION in found


def test_the_removed_search_endpoint_is_not_called(client, stub_esi):
    """A regression guard rather than a style check.

    `/search/` answers 404 for everything now and the failure is silent — the
    result is simply an empty list — so nothing downstream would go red if a
    future edit reinstated it. Asserting the replacement is *also* called keeps
    this from passing against a version that dropped the lookup entirely.
    """
    client.get("/api/suggest-station?q=PR-8CA")

    urls = [u for _, u in stub_esi]
    # Matched on `/latest/search` specifically, not on `/search/` anywhere: the
    # authenticated `/latest/characters/{id}/search/` was **not** removed and
    # still supplies player structures, so a looser pattern would ban the one
    # member of the family that still works.
    assert not any(u.rstrip("/").endswith("/latest/search") for u in urls), (
        f"the removed public /search/ endpoint was called: {urls}")
    assert any("/universe/ids/" in u for u in urls)


def test_a_name_matching_nothing_is_not_an_error(client, monkeypatch):
    """`/universe/ids/` answers an unmatched name with an empty object and a
    200, so there is no error case to mistake for one."""
    class _Empty(_FakeClient):
        async def post(self, url, **kw):
            self.calls.append(("POST", url))
            return _Resp({})

    monkeypatch.setattr(loc, "esi_client", lambda **kw: _Empty([]))
    monkeypatch.setattr(loc, "_locations_in_system", lambda sid: [])

    r = client.get("/api/suggest-station?q=zzqq-no-such-place")

    assert r.status_code == 200
    assert r.json()["other"] == [] and r.json()["owned"] == []


def test_a_short_query_asks_esi_nothing(client, stub_esi):
    """One character is a keystroke on the way to a word, not a search."""
    r = client.get("/api/suggest-station?q=P")

    assert r.status_code == 200
    assert stub_esi == [], f"ESI was called for a one-character query: {stub_esi}"
