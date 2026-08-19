"""The background sync loop: keep the caches warm, and say what changed.

Step 4 of `docs/design-hosted-v2.md`. Two jobs, and the second is the one that
shapes the design — the caches alone would be satisfied by any loop at all,
while the event log (see `app/sync/events.py`) is what §9.5's Discord bot needs
and what a cache cannot give it.

**Delay after completion, not on a fixed schedule.** A fixed interval with a
slow fetch means the next round starts while the last is still running, and a
loop that overlaps itself against a rate-limited API digs its own hole. Waiting
a fixed gap *after* finishing cannot overlap by construction.

**Jitter, because N characters otherwise move in lockstep.** They are seeded at
the same moment — process start — so without it every character's round comes
due in the same second, forever, and the load is a spike with a flat line after
it rather than a trickle. The jitter is proportional, so it stays meaningful
whatever the interval is set to.

**Failure is not special.** A character whose token was revoked will fail every
round; the ESI transport already stops putting its requests on the wire (see
the quarantine in `app/esi/client.py`), so the loop does not need its own
backoff for that case and does not have one. What it does is record the failure
as an event and carry on to the next character — one broken pilot must not stop
the other three from syncing.

**What counts as changed.** A fingerprint of the fetched collection, not the
JSON. ESI does not promise a stable order, so comparing raw bodies would emit
on every reshuffle and the log would be noise nobody reads. Counting alone is
worse: an item swapped for another is a real change with an unchanged count.
The fingerprint is the sorted identity of the collection, hashed — cheap,
order-independent, and it moves if and only if the set moved.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import time
from typing import Iterable, Optional

from app.auth.token_store import list_characters, update_corporation_id, update_last_sync
from app.character.assets import fetch_assets, fetch_corp_assets
from app.character.blueprints import fetch_blueprints
from app.character.skills import fetch_skills
from app.db.conn import connect
from app.esi.client import esi_client
from app.sync import events
from app.web.deps import _valid_token_async

#: Gap between the end of one round and the start of the next, per character.
#: Fifteen minutes is well inside every relevant ESI cache header, so a shorter
#: one would mostly re-fetch answers the server told us not to ask for again.
DEFAULT_INTERVAL = 15 * 60.0

#: Fraction of the interval spread randomly across characters. ±20% of fifteen
#: minutes is a six-minute spread, which is enough to unstack four characters
#: without making any of them noticeably staler.
JITTER = 0.2

#: The first round does not wait a full interval — a freshly started process
#: should fill its caches — but it does stagger, so N characters do not all
#: fetch at second zero.
FIRST_ROUND_SPREAD = 30.0


def _fingerprint(items: Iterable, key=None) -> str:
    """Order-independent identity of a collection.

    `key` pulls the identity out of one item; the default handles the two shapes
    the fetchers return — dicts with an `item_id`/`type_id`, and objects with the
    same attributes.
    """
    def _identity(item):
        if key is not None:
            return key(item)
        for name in ("item_id", "type_id", "job_id", "order_id"):
            if isinstance(item, dict) and name in item:
                return item[name]
            got = getattr(item, name, None)
            if got is not None:
                return got
        # Nothing identifying: fall back to the whole item, sorted so a dict
        # whose keys came back in a different order still hashes the same.
        return json.dumps(item, sort_keys=True, default=str)

    # `fetch_skills` returns {skill_id: level}, where the *level* is the thing
    # that changes — iterating a mapping yields only its keys, so training a
    # skill from IV to V would look like nothing happened.
    parts = (f"{k}={v}" for k, v in items.items()) if isinstance(items, dict)         else (str(_identity(i)) for i in items)
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:32]


class SyncWorker:
    """One task, looping over every character in turn.

    Deliberately not one task per character: they share an ESI budget and a
    SQLite writer, so running them concurrently buys latency the user cannot
    see and costs contention they can.
    """

    def __init__(self, interval: float = DEFAULT_INTERVAL,
                 jitter: float = JITTER,
                 clock=time.monotonic,
                 sleep=asyncio.sleep) -> None:
        self.interval = interval
        self.jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        #: character_id -> when its next round is due (clock units)
        self._due: dict[int, float] = {}
        #: (character_id, what) -> fingerprint, so a round can tell change from
        #: repetition without re-reading what it just wrote.
        self._seen: dict[tuple[int, str], str] = {}
        self.rounds = 0
        self.failures = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="eve-sync-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── the loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Forever: sync whoever is due, then wait until somebody is."""
        while not self._stop.is_set():
            try:
                waited = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # never let the loop die
                print(f"[sync] worker tick failed: {exc}", flush=True)
                waited = 60.0
            if waited > 0:
                await self._sleep(waited)

    async def tick(self) -> float:
        """Sync every character that is due. Returns how long to wait next.

        Split out from `run` so a test can drive one round without a clock.
        """
        with connect() as conn:
            # token_store speaks sqlite3, not SQLAlchemy — it is one of the ten
            # modules the query conversion has not reached yet. The driver
            # connection underneath is the same one, so this stays a single
            # connection rather than a second opinion about the database.
            chars = list_characters(conn.connection.driver_connection)
        if not chars:
            return self.interval

        now = self._clock()
        for index, (char_id, name) in enumerate(chars):
            due = self._due.get(char_id)
            if due is None:
                # First sight: stagger rather than firing all of them at once.
                due = now + (index * FIRST_ROUND_SPREAD / max(len(chars), 1))
                self._due[char_id] = due
            if due > now:
                continue
            await self.sync_character(char_id, name)
            self._due[char_id] = self._clock() + self._next_delay()

        self.rounds += 1
        soonest = min(self._due.values()) if self._due else now + self.interval
        return max(0.0, min(soonest - self._clock(), self.interval))

    def _next_delay(self) -> float:
        spread = self.interval * self.jitter
        return self.interval + random.uniform(-spread, spread)

    # ── one character ────────────────────────────────────────────────────────

    async def sync_character(self, char_id: int, name: str = "") -> bool:
        """Refresh one character's caches and record what moved.

        Returns whether it got as far as writing anything. Never raises: a
        broken character must not stop the ones after it.
        """
        try:
            token = await _valid_token_async(char_id)
        except Exception as exc:
            return self._record_failure(char_id, f"token refresh failed: {exc}")
        if not token:
            return self._record_failure(char_id, "no valid token")

        changed: list[tuple[str, dict]] = []
        corp_id: int | None = None
        try:
            with connect() as conn:
                # The fetchers write their caches through the DBAPI connection
                # underneath this one, so the events emitted below land after
                # the data they describe on the same connection. A consumer is
                # never told about a change it cannot read; the reverse — data
                # visible before its event — is only a late announcement.
                raw = conn.connection.driver_connection
                async with esi_client() as client:
                    blueprints = await fetch_blueprints(client, char_id, token, raw)
                    changed += self._diff(char_id, "blueprints", blueprints)

                    assets = await fetch_assets(client, char_id, token, raw)
                    changed += self._diff(char_id, "assets", assets)

                    skills = await fetch_skills(client, char_id, token, raw)
                    changed += self._diff(char_id, "skills", skills)

                    try:
                        corp_id, corp_assets = await fetch_corp_assets(
                            client, char_id, token, raw)
                        if corp_id:
                            update_corporation_id(raw, char_id, corp_id)
                            changed += self._diff(
                                char_id, "corp_assets", corp_assets,
                                corporation_id=corp_id)
                    except Exception as exc:
                        # Corp assets need a role most characters do not have.
                        # Not a failure of the character, so it is not recorded
                        # as one — the other three fetches already landed.
                        print(f"[sync] corp assets skipped for {char_id}: {exc}",
                              flush=True)

                update_last_sync(raw, char_id)

                # The events go in the same transaction as the caches they
                # describe, so a consumer is never told about a change it
                # cannot yet read.
                for kind, detail in changed:
                    events.emit(conn, kind, character_id=char_id,
                                corporation_id=corp_id if "corporation" in kind else None,
                                detail=detail)
                events.trim(conn)
                conn.commit()
        except Exception as exc:
            return self._record_failure(char_id, str(exc))

        if changed:
            print(f"[sync] {name or char_id}: "
                  + ", ".join(k.rsplit('.', 2)[-2] for k, _ in changed), flush=True)
        return True

    def _diff(self, char_id: int, what: str, items,
              corporation_id: int | None = None) -> list[tuple[str, dict]]:
        """One event if this collection's identity moved, none otherwise."""
        if items is None:
            return []
        # Not listified: `fetch_skills` returns {skill_id: level} and list()
        # would throw the levels away, which is the half that changes.
        size = len(items)
        fingerprint = _fingerprint(items)
        key = (char_id, what)
        previous = self._seen.get(key)
        self._seen[key] = fingerprint
        if previous is None or previous == fingerprint:
            # First sight is not a change: a process restart must not announce
            # everything the account owns as newly acquired.
            return []
        kind = ("corporation.assets.changed" if what == "corp_assets"
                else f"character.{what}.changed")
        return [(kind, {"count": size})]

    def _record_failure(self, char_id: int, reason: str) -> bool:
        self.failures += 1
        print(f"[sync] character {char_id} failed: {reason}", flush=True)
        try:
            with connect() as conn:
                events.emit(conn, "sync.character.failed", character_id=char_id,
                            detail={"reason": reason[:200]})
                conn.commit()
        except Exception:
            pass                                   # the log is not worth a crash
        return False


_WORKER: Optional[SyncWorker] = None


def enabled() -> bool:
    """Off only when explicitly switched off.

    Default-on because a hosted deployment without it is a set of caches nobody
    refreshes; `tests/conftest.py` turns it off, and so can a developer who does
    not want background ESI traffic.
    """
    return (os.environ.get("EVE_SYNC_WORKER", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def start(**kwargs) -> Optional[SyncWorker]:
    """Start the process-wide worker, if it is enabled and not already running."""
    global _WORKER
    if not enabled():
        return None
    if _WORKER is None:
        _WORKER = SyncWorker(**kwargs)
    _WORKER.start()
    return _WORKER


async def stop() -> None:
    global _WORKER
    if _WORKER is not None:
        await _WORKER.stop()
        _WORKER = None


def status() -> dict:
    """What the worker has been doing. For a health view, and for tests."""
    if _WORKER is None:
        return {"running": False, "enabled": enabled()}
    return {
        "running": _WORKER.running,
        "enabled": True,
        "rounds": _WORKER.rounds,
        "failures": _WORKER.failures,
        "characters_tracked": len(_WORKER._due),
    }
