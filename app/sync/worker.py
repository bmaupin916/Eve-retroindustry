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
from app.character.assets import (
    container_item_ids,
    fetch_assets,
    fetch_container_names,
    fetch_corp_assets,
)
from app.character.blueprints import fetch_blueprints
from app.character.jobs import fetch_industry_jobs
from app.character.planets import fetch_colonies
from app.character.orders import (
    fetch_corp_orders,
    fetch_corp_orders_history,
    fetch_orders,
    fetch_orders_history,
)
from app.character.contracts import (
    fetch_character_contracts,
    fetch_corp_contracts,
)
from app.character.wallet import (
    fetch_balance,
    fetch_corp_journal,
    fetch_corp_transactions,
    fetch_corp_wallets,
    fetch_journal,
    fetch_transactions,
)
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

#: How long to wait when there is nobody to sync at all. A full interval here
#: was a real defect: an instance that starts with no characters — a fresh
#: deployment, or this one right after the test-data cleanup — found nobody on
#: its first tick, slept the whole fifteen minutes, and had no way to be told
#: that somebody had just signed in. `/jobs` reads only the cache, so it
#: answered "not synced yet" for a quarter of an hour after a successful login.
IDLE_INTERVAL = 60.0


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
        #: Set by wake() to cut a sleep short. Adding a character is the case
        #: this exists for: without it the new character waits out whatever
        #: remains of the current interval, up to the full fifteen minutes.
        self._wake = asyncio.Event()
        #: character_id -> when its next round is due (clock units)
        self._due: dict[int, float] = {}
        #: (character_id, what) -> fingerprint, so a round can tell change from
        #: repetition without re-reading what it just wrote.
        self._seen: dict[tuple[int, str], str] = {}
        #: Job ids by status, per character. Jobs are tracked by transition
        #: rather than by fingerprint — see `_job_events`.
        self._jobs_active: dict[int, set] = {}
        self._jobs_finished: dict[int, set] = {}
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

    def wake(self) -> None:
        """Ask for a round now instead of when the current sleep expires.

        Safe to call from anywhere, including when the worker is mid-round or
        not running: the flag is level-triggered and cleared by the next wait,
        so a wake that arrives during a round still shortens the sleep after it.
        """
        self._wake.set()

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
                await self._wait(waited)

    async def _wait(self, seconds: float) -> bool:
        """Sleep for `seconds`, or until wake() is called. True if woken early.

        The nap is the *injected* sleep, so tests keep driving time by hand;
        only the racing of it against the wake flag is new. Both sides are
        awaited after cancellation so a cut-short sleep leaves nothing pending.
        """
        if seconds <= 0:
            return False
        nap = asyncio.ensure_future(self._sleep(seconds))
        woke = asyncio.ensure_future(self._wake.wait())
        done, pending = await asyncio.wait(
            {nap, woke}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._wake.clear()
        return woke in done

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
            # Poll rather than sleep the interval. wake() is the fast path when
            # a login happens in this process; this is the backstop for every
            # other way a character can appear, and for a wake that races the
            # round that is already running.
            return min(self.interval, IDLE_INTERVAL)

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
                    # `force_refresh` on all three, like the jobs fetch below.
                    # Without it each consults the same TTL it is about to
                    # write, which is circular — and blueprints' TTL is fifteen
                    # minutes against a fifteen-minute tick, so whether the
                    # worker refreshed them at all came down to scheduling
                    # jitter. Assets' ten-minute TTL always lost the race and
                    # so always refreshed, which is why this never showed up as
                    # a stale asset list.
                    blueprints = await fetch_blueprints(client, char_id, token, raw,
                                                        force_refresh=True)
                    changed += self._diff(char_id, "blueprints", blueprints)

                    assets = await fetch_assets(client, char_id, token, raw,
                                                force_refresh=True)
                    changed += self._diff(char_id, "assets", assets)

                    # Custom container and ship names, in one POST. Derived
                    # from the assets just fetched rather than from a category
                    # list: an item is a container exactly when something else
                    # is inside it. No event — renaming a can is not news.
                    await fetch_container_names(
                        client, char_id, token, container_item_ids(assets or []),
                        conn=raw)

                    skills = await fetch_skills(client, char_id, token, raw)
                    changed += self._diff(char_id, "skills", skills)

                    # /jobs reads this cache and never calls ESI itself, so a
                    # failed fetch here is the difference between a stale page
                    # and a page that says "not synced yet" — hence None rather
                    # than [] when the fetch could not run.
                    jobs = await fetch_industry_jobs(client, char_id, token,
                                                     conn=raw, force_refresh=True)
                    if jobs is not None:
                        changed += self._job_events(char_id, jobs)

                    # /orders reads these and never calls ESI itself. Active
                    # and history are separate rows because the page asks for
                    # one or the other and history is the expensive one — four
                    # paginated calls against ninety days that change once a
                    # trade closes.
                    orders = await fetch_orders(client, char_id, token, conn=raw)
                    changed += self._diff(char_id, "orders", orders)

                    # No event for history: a closed order is not news the
                    # way a new one is, and the page that reads it is a ledger
                    # rather than a monitor. It writes nothing when the fetch
                    # fails, so the previous cache stays the best answer.
                    await fetch_orders_history(client, char_id, token, conn=raw)

                    # /wallet reads these. The balance goes to the table the
                    # dashboard already polls, so that read stops falling
                    # through to ESI as a side effect of this one.
                    await fetch_balance(client, char_id, token, conn=raw)
                    journal = await fetch_journal(client, char_id, token,
                                                  conn=raw)
                    changed += self._diff(char_id, "wallet", journal)
                    await fetch_transactions(client, char_id, token, conn=raw)

                    # /contracts reads this. Contract *items* are deliberately
                    # not prefetched: they never change once a contract exists,
                    # so they are cached on first expand instead — a character
                    # with fifty contracts would otherwise cost fifty calls a
                    # tick to store something most of which is never opened.
                    contracts = await fetch_character_contracts(
                        client, char_id, token, conn=raw)
                    changed += self._diff(char_id, "contracts", contracts)

                    # Colonies: one list call plus one per planet. Expensive
                    # enough that doing it per page view was the worst offender
                    # in the app — and pointless, because what the pages want
                    # from it is an extractor expiry, which is a fixed future
                    # timestamp until somebody resets the program.
                    colonies = await fetch_colonies(client, char_id, token,
                                                    conn=raw)
                    if isinstance(colonies, tuple):
                        changed += self._diff(char_id, "colonies", colonies[0])

                    try:
                        corp_id, corp_assets = await fetch_corp_assets(
                            client, char_id, token, raw, force_refresh=True)
                        if corp_id:
                            update_corporation_id(raw, char_id, corp_id)
                            changed += self._diff(
                                char_id, "corp_assets", corp_assets,
                                corporation_id=corp_id)
                            await fetch_container_names(
                                client, corp_id, token,
                                container_item_ids(corp_assets or []),
                                conn=raw, corporate=True)
                            # Same role requirement as corp assets and the same
                            # best-effort handling: a 403 returns an error
                            # string rather than raising, and writes nothing.
                            corp_orders, _err = await fetch_corp_orders(
                                client, corp_id, token, conn=raw)
                            changed += self._diff(
                                char_id, "corp_orders", corp_orders,
                                corporation_id=corp_id)
                            await fetch_corp_orders_history(
                                client, corp_id, token, conn=raw)

                            # Corporation wallets: one call lists every
                            # division's balance, then the ledgers are per
                            # division. Only the divisions ESI actually
                            # reported are walked — asking for all seven on a
                            # corp that uses two spends ten paginated calls a
                            # tick to cache nothing.
                            wallets, _werr = await fetch_corp_wallets(
                                client, corp_id, token, conn=raw)
                            for wallet in wallets or []:
                                div = wallet.get("division")
                                if not div:
                                    continue
                                await fetch_corp_journal(
                                    client, corp_id, div, token, conn=raw)
                                await fetch_corp_transactions(
                                    client, corp_id, div, token, conn=raw)

                            corp_contracts, _cerr = await fetch_corp_contracts(
                                client, corp_id, token, conn=raw)
                            changed += self._diff(
                                char_id, "corp_contracts", corp_contracts,
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
        kind = ({"corp_assets": "corporation.assets.changed",
                 "corp_orders": "corporation.orders.changed",
                 "corp_contracts": "corporation.contracts.changed"}.get(what)
                or f"character.{what}.changed")
        return [(kind, {"count": size})]

    def _job_events(self, char_id: int, jobs: list) -> list[tuple[str, dict]]:
        """Jobs are the one collection where the *transition* is the news.

        "Assets changed" is a fact about a set; "a job finished" is something a
        person acts on, and it is exactly what a cache cannot tell you after the
        fact — a delivered job leaves ESI's window and the set simply shrinks.
        So the ids are tracked per status rather than as one collection.
        """
        out: list[tuple[str, dict]] = []
        active = {j.get("job_id") for j in jobs
                  if j.get("status") == "active" and j.get("job_id")}
        finished = {j.get("job_id") for j in jobs
                    if j.get("status") in ("ready", "delivered") and j.get("job_id")}

        was_active = self._jobs_active.get(char_id)
        self._jobs_active[char_id] = active
        was_finished = self._jobs_finished.get(char_id)
        self._jobs_finished[char_id] = finished
        if was_active is None:
            return out                       # first sight is not news

        started = active - was_active
        if started:
            out.append(("job.started", {"count": len(started)}))
        # Newly finished: either it appeared in the finished set, or it left the
        # active set without appearing anywhere — ESI drops delivered jobs after
        # a while, and a job that vanishes has finished, not been cancelled.
        completed = (finished - (was_finished or set())) | (was_active - active - started)
        if completed:
            out.append(("job.completed", {"count": len(completed)}))
        return out

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


def wake() -> bool:
    """Ask the process-wide worker for a round now. False if there is none.

    Called when a character is added, which is the moment the caches are most
    obviously wrong and the moment the user is most likely to be looking.
    """
    if _WORKER is None or not _WORKER.running:
        return False
    _WORKER.wake()
    return True


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
        # So a health view can say how stale is too stale without hardcoding a
        # number that would then disagree with the worker after a config change.
        "interval": _WORKER.interval,
    }
