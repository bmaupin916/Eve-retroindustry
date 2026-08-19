"""The background sync loop.

Two jobs — keep the caches warm, and record what moved — and the second is the
one that shapes it, because §9.5's Discord bot cannot get transitions from a
cache.

Everything here drives the worker with an injected clock and an injected sleep,
so a fifteen-minute interval takes no wall-clock time and the scheduling is
deterministic. Nothing reaches ESI: the fetchers are stubbed at the module the
worker imports them into.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.db import conn as db
from app.db.schema import apply_schema
from app.sync import events, worker as w


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def db_with(tmp_path, monkeypatch):
    """A database with N characters and nothing else."""
    monkeypatch.setenv("EVE_DATABASE_URL", f"sqlite:///{tmp_path / 'w.db'}")
    db.dispose()

    def _make(count=2):
        with db.connect() as c:
            raw = c.connection.driver_connection
            apply_schema(raw)
            now = time.time()
            for i in range(count):
                raw.execute(
                    "INSERT INTO characters (character_id, character_name,"
                    " refresh_token, access_token, token_expires_at,"
                    " corporation_id, last_sync_at, added_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (900_000_000 + i, f"Pilot {i}", "r", "a", now + 10**6,
                     98000001, now, now + i))
            raw.commit()
        return count

    yield _make
    db.dispose()


@pytest.fixture
def stub_esi(monkeypatch):
    """Replace every fetcher. Returns a dict the test mutates to change answers."""
    state = {
        "blueprints": [{"item_id": 1, "type_id": 641}],
        "assets": [{"item_id": 10, "type_id": 34}],
        "skills": {3380: 4},
        "corp_assets": (98000001, [{"item_id": 100, "type_id": 34}]),
        "token": "tok",
        "calls": [],
    }

    async def _token(char_id):
        state["calls"].append(("token", char_id))
        if isinstance(state["token"], Exception):
            raise state["token"]
        return state["token"]

    async def _blueprints(client, char_id, token, conn, **kw):
        state["calls"].append(("blueprints", char_id))
        return state["blueprints"]

    async def _assets(client, char_id, token, conn, **kw):
        state["calls"].append(("assets", char_id))
        return state["assets"]

    async def _skills(client, char_id, token, conn, **kw):
        state["calls"].append(("skills", char_id))
        return state["skills"]

    async def _corp(client, char_id, token, conn, **kw):
        state["calls"].append(("corp_assets", char_id))
        return state["corp_assets"]

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(w, "_valid_token_async", _token)
    monkeypatch.setattr(w, "fetch_blueprints", _blueprints)
    monkeypatch.setattr(w, "fetch_assets", _assets)
    monkeypatch.setattr(w, "fetch_skills", _skills)
    monkeypatch.setattr(w, "fetch_corp_assets", _corp)
    monkeypatch.setattr(w, "esi_client", lambda *a, **k: _Client())
    monkeypatch.setattr(w, "update_corporation_id", lambda *a, **k: None)
    monkeypatch.setattr(w, "update_last_sync", lambda *a, **k: None)
    return state


def _events():
    with db.connect() as c:
        return events.since(c)


def _worker(clock, **kw):
    async def _no_sleep(_seconds):
        return None
    return w.SyncWorker(interval=900.0, clock=clock, sleep=_no_sleep, **kw)


# ── what it does ─────────────────────────────────────────────────────────────

def test_a_round_fetches_every_character(db_with, stub_esi):
    """Over the first-round stagger, not inside a single tick — the stagger is
    the point, and a tick only syncs whoever is due by then."""
    db_with(2)
    clock = _Clock()
    worker = _worker(clock)

    asyncio.run(worker.tick())              # character one, immediately
    clock.advance(60)
    asyncio.run(worker.tick())              # character two, once it comes due

    fetched = {c for kind, c in stub_esi["calls"] if kind == "assets"}
    assert fetched == {900_000_000, 900_000_001}


def test_the_first_sight_of_a_character_is_not_a_change(db_with, stub_esi):
    """A restart must not announce everything the account owns as newly
    acquired. The bot would post four times about nothing."""
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    clock.advance(60)
    asyncio.run(worker.tick())

    assert _events() == [], "the first sync emitted change events"


def test_a_changed_collection_emits_exactly_one_event(db_with, stub_esi):
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    clock.advance(60)
    asyncio.run(worker.tick())                       # baseline

    stub_esi["assets"] = [{"item_id": 10, "type_id": 34},
                          {"item_id": 11, "type_id": 35}]
    clock.advance(2000)
    asyncio.run(worker.tick())

    kinds = [e.kind for e in _events()]
    assert kinds == ["character.assets.changed"], kinds
    assert _events()[0].detail == {"count": 2}
    assert _events()[0].character_id == 900_000_000


def test_an_unchanged_collection_emits_nothing(db_with, stub_esi):
    """The caches are refreshed every round; the log is not a round counter."""
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    clock.advance(60)
    asyncio.run(worker.tick())
    for _ in range(3):
        clock.advance(2000)
        asyncio.run(worker.tick())

    assert _events() == []


def test_a_reordered_collection_is_not_a_change(db_with, stub_esi):
    """ESI promises no order. Comparing bodies would emit every round and the
    log would be noise nobody reads."""
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    stub_esi["assets"] = [{"item_id": 1}, {"item_id": 2}, {"item_id": 3}]
    clock.advance(60)
    asyncio.run(worker.tick())

    stub_esi["assets"] = [{"item_id": 3}, {"item_id": 1}, {"item_id": 2}]
    clock.advance(2000)
    asyncio.run(worker.tick())

    assert _events() == []


def test_a_swap_that_keeps_the_count_is_still_a_change(db_with, stub_esi):
    """Counting alone would miss this, and it is a real event: one item sold,
    another bought."""
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    stub_esi["assets"] = [{"item_id": 1}, {"item_id": 2}]
    clock.advance(60)
    asyncio.run(worker.tick())

    stub_esi["assets"] = [{"item_id": 1}, {"item_id": 99}]
    clock.advance(2000)
    asyncio.run(worker.tick())

    assert [e.kind for e in _events()] == ["character.assets.changed"]


def test_a_trained_skill_is_a_change(db_with, stub_esi):
    """Skills come back as {id: level}. Iterating a mapping yields its keys, so
    IV to V on a skill already known would look like nothing happened."""
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    stub_esi["skills"] = {3380: 4}
    clock.advance(60)
    asyncio.run(worker.tick())

    stub_esi["skills"] = {3380: 5}
    clock.advance(2000)
    asyncio.run(worker.tick())

    assert [e.kind for e in _events()] == ["character.skills.changed"]


def test_corp_assets_are_reported_against_the_corporation(db_with, stub_esi):
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    clock.advance(60)
    asyncio.run(worker.tick())
    stub_esi["corp_assets"] = (98000001, [{"item_id": 100}, {"item_id": 101}])
    clock.advance(2000)
    asyncio.run(worker.tick())

    e = _events()[0]
    assert e.kind == "corporation.assets.changed"
    assert e.corporation_id == 98000001


# ── scheduling ───────────────────────────────────────────────────────────────

def test_a_character_is_not_synced_twice_in_one_interval(db_with, stub_esi):
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    clock.advance(60)
    asyncio.run(worker.tick())
    before = len(stub_esi["calls"])

    clock.advance(10)                        # nowhere near the interval
    asyncio.run(worker.tick())
    assert len(stub_esi["calls"]) == before, "a character synced again immediately"


def test_the_next_round_is_jittered_so_characters_do_not_stack(db_with, stub_esi):
    """Seeded at the same moment, characters would otherwise come due in the
    same second forever — a spike every interval instead of a trickle."""
    db_with(4)
    clock = _Clock()
    worker = _worker(clock)

    asyncio.run(worker.tick())              # first character
    clock.advance(60)
    asyncio.run(worker.tick())              # the other three, now due

    due = sorted(worker._due.values())
    assert len(set(due)) == 4, f"all four came due together: {due}"
    spread = due[-1] - due[0]
    assert spread > 0, "no jitter at all"
    assert spread < worker.interval, "jitter exceeded a whole interval"


def test_the_first_round_staggers_rather_than_firing_at_once(db_with, stub_esi):
    """A fresh process should fill its caches, so the first character goes
    immediately — but four at once is the stampede the stagger exists to
    prevent, so the rest are spread over the next half-minute."""
    db_with(4)
    clock = _Clock()
    worker = _worker(clock)

    asyncio.run(worker.tick())

    fetched = {c for kind, c in stub_esi["calls"] if kind == "assets"}
    assert fetched == {900_000_000}, f"more than one fetched at once: {fetched}"

    pending = sorted(v for k, v in worker._due.items() if k != 900_000_000)
    assert len(set(pending)) == 3, f"the other three came due together: {pending}"
    assert max(pending) - clock.now <= w.FIRST_ROUND_SPREAD


def test_the_wait_it_asks_for_never_exceeds_the_interval(db_with, stub_esi):
    db_with(2)
    clock = _Clock()
    worker = _worker(clock)

    for _ in range(3):
        wait = asyncio.run(worker.tick())
        assert 0 <= wait <= worker.interval
        clock.advance(wait + 1)


def test_no_characters_means_no_work(db_with, stub_esi):
    db_with(0)
    clock = _Clock()
    worker = _worker(clock)

    assert asyncio.run(worker.tick()) == worker.interval
    assert stub_esi["calls"] == []


# ── failure ──────────────────────────────────────────────────────────────────

def test_one_broken_character_does_not_stop_the_others(db_with, stub_esi,
                                                       monkeypatch):
    """A revoked token fails every round forever. The other pilots still sync."""
    db_with(2)
    clock = _Clock()
    worker = _worker(clock)
    calls = stub_esi["calls"]

    async def _token(char_id):
        calls.append(("token", char_id))
        if char_id == 900_000_000:
            raise RuntimeError("token revoked")
        return "tok"

    monkeypatch.setattr(w, "_valid_token_async", _token)

    asyncio.run(worker.tick())              # the broken one, first
    clock.advance(60)
    asyncio.run(worker.tick())              # the working one, once due

    synced = {c for kind, c in calls if kind == "assets"}
    assert synced == {900_000_001}, "the second character did not get its turn"
    assert worker.failures >= 1


def test_a_failure_is_recorded_as_an_event(db_with, stub_esi):
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)
    stub_esi["token"] = None                 # no valid token

    clock.advance(60)
    asyncio.run(worker.tick())

    got = _events()
    assert [e.kind for e in got] == ["sync.character.failed"]
    assert "no valid token" in got[0].detail["reason"]


def test_a_fetch_that_raises_is_recorded_and_survived(db_with, stub_esi,
                                                     monkeypatch):
    db_with(1)
    clock = _Clock()
    worker = _worker(clock)

    async def _boom(client, char_id, token, conn, **kw):
        raise RuntimeError("ESI exploded")

    monkeypatch.setattr(w, "fetch_assets", _boom)
    clock.advance(60)
    asyncio.run(worker.tick())

    assert [e.kind for e in _events()] == ["sync.character.failed"]
    assert worker.failures == 1


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_the_worker_is_default_on_and_switchable_off(monkeypatch):
    monkeypatch.delenv("EVE_SYNC_WORKER", raising=False)
    assert w.enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("EVE_SYNC_WORKER", off)
        assert w.enabled() is False, off
    monkeypatch.setenv("EVE_SYNC_WORKER", "1")
    assert w.enabled() is True


def test_start_is_a_no_op_when_it_is_switched_off(monkeypatch):
    monkeypatch.setenv("EVE_SYNC_WORKER", "0")
    assert w.start() is None
    assert w.status() == {"running": False, "enabled": False}


def test_the_loop_starts_and_stops(db_with, stub_esi, monkeypatch):
    db_with(1)
    monkeypatch.setenv("EVE_SYNC_WORKER", "1")

    async def scenario():
        worker = w.SyncWorker(interval=0.01, jitter=0.0)
        worker.start()
        assert worker.running
        await asyncio.sleep(0.05)
        await worker.stop()
        return worker

    worker = asyncio.run(scenario())
    assert not worker.running
    assert worker.rounds > 0, "the loop never completed a round"


def test_stopping_twice_is_harmless(db_with, stub_esi):
    async def scenario():
        worker = w.SyncWorker(interval=0.01)
        worker.start()
        await asyncio.sleep(0.02)
        await worker.stop()
        await worker.stop()

    asyncio.run(scenario())
