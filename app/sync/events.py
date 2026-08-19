"""What changed, written down — not just the new value.

The sync worker refreshes caches. The web UI needs nothing more than that: it
re-reads the cache on every page load, so "the newest value" is the whole
answer. A notifier is different. §9.5 of the design doc wants a Discord bot
that announces a finished job, a filled order, an expiring extractor — and a
bot that polls a cache for changes misses them. Two changes between two polls
look like one. A change that reverts between polls looks like none. The
interesting event is often the *transition*, and a cache does not keep
transitions.

So the worker records them, and the doc is explicit that retrofitting this
costs more than building it in.

**Shape.** An append-only table with a monotonic id. A consumer keeps a cursor
and asks for `id > cursor`, which answers "what did I miss while I was down?"
exactly. Nothing else considered has that property on both backends: an
in-process signal dies with the process and is invisible to the bot, which is
a second process; Postgres LISTEN/NOTIFY does not exist on SQLite, and this
schema has to emit for both today. NOTIFY is a good wake-up to add later — it
saves polling latency — but the log stays the source of truth underneath it.

**What goes in one.** What changed, and enough to say it out loud. Never the
payload: a consumer that wants the new asset list reads the cache, which is
authoritative and singular. An event carrying its own copy is a second answer
that can disagree with the first.

**Retention.** The log grows forever unless something trims it, and nothing
consumes it yet. `trim()` keeps a bounded tail; the worker calls it. When the
bot exists it will need to move to "keep everything after the slowest cursor"
instead, which is a change to make then rather than a mechanism to build now.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Event kinds, listed so they are a vocabulary rather than free text. A typo
#: in a kind is a subscriber that silently never fires, which is the failure
#: mode this whole module exists to avoid.
KINDS = frozenset({
    "character.added",
    "character.removed",
    "character.assets.changed",
    "character.blueprints.changed",
    "character.skills.changed",
    "character.wallet.changed",
    "character.orders.changed",
    "character.contracts.changed",
    "character.planets.changed",
    "corporation.assets.changed",
    "job.started",
    "job.completed",
    "pi.extractor.expiring",
    "sync.character.failed",
    "sync.character.quarantined",
})

#: Rows kept by `trim()`. Generous: the log is small (a row is well under 200
#: bytes) and the cost of trimming too eagerly is a consumer that missed
#: something, which is the one failure this design is for.
DEFAULT_KEEP = 20_000


@dataclass(frozen=True)
class Event:
    id: int
    created_at: int
    kind: str
    character_id: Optional[int]
    corporation_id: Optional[int]
    detail: dict

    @property
    def when(self) -> float:
        return float(self.created_at)


def emit(conn: Connection, kind: str, *, character_id: int | None = None,
         corporation_id: int | None = None, detail: dict | None = None,
         now: float | None = None) -> int:
    """Append one event and return its id.

    Does not commit. The caller decides the transaction, which matters: an
    event announcing a cache update has to land in the same commit as the
    update, or a consumer can be told about a change it cannot yet see.
    """
    if kind not in KINDS:
        raise ValueError(
            f"unknown event kind {kind!r}. Add it to KINDS — a kind that only "
            "exists at the call site is a subscriber that never fires."
        )
    return conn.execute(
        text("INSERT INTO sync_events"
             " (created_at, kind, character_id, corporation_id, detail_json)"
             " VALUES (:created_at, :kind, :character_id, :corporation_id, :detail)"
             " RETURNING id"),
        {
            "created_at": int(now if now is not None else time.time()),
            "kind": kind,
            "character_id": character_id,
            "corporation_id": corporation_id,
            "detail": json.dumps(detail or {}, default=str),
        },
    ).scalar()


def since(conn: Connection, cursor: int = 0, *, limit: int = 500,
          kinds: set[str] | None = None) -> list[Event]:
    """Events after `cursor`, oldest first — the consumer's whole interface.

    Ordered by id rather than by time on purpose: two events written in the
    same second have no order by timestamp, and a consumer that advances its
    cursor by time would either repeat one or skip one.
    """
    sql = ("SELECT id, created_at, kind, character_id, corporation_id, detail_json"
           " FROM sync_events WHERE id > :cursor")
    params: dict = {"cursor": cursor, "limit": limit}
    if kinds:
        # Sorted so the statement text is stable and the driver can cache it.
        names = sorted(kinds)
        placeholders = ", ".join(f":k{i}" for i in range(len(names)))
        sql += f" AND kind IN ({placeholders})"
        params.update({f"k{i}": n for i, n in enumerate(names)})
    sql += " ORDER BY id LIMIT :limit"

    rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_event(r) -> Event:
    """The one place a row becomes an Event. Both readers select the same six
    columns in the same order, and this is what keeps that true."""
    return Event(
        id=r[0], created_at=r[1], kind=r[2],
        character_id=r[3], corporation_id=r[4],
        detail=json.loads(r[5]) if r[5] else {},
    )


def recent(conn: Connection, limit: int = 50) -> list[Event]:
    """The newest `limit` events, newest first. For looking, not for consuming.

    Deliberately not `since(conn, 0, limit=…)`: that answers "the oldest N after
    my cursor", which is right for a subscriber working forwards and wrong for
    a human who wants to know what just happened. Nor `latest_id() - limit`,
    because `trim()` leaves gaps and that arithmetic would quietly return fewer
    rows than asked for. A consumer that tracks a cursor still wants `since`.
    """
    rows = conn.execute(
        text("SELECT id, created_at, kind, character_id, corporation_id,"
             " detail_json FROM sync_events ORDER BY id DESC LIMIT :limit"),
        {"limit": limit},
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def latest_id(conn: Connection) -> int:
    """The newest id, or 0. A consumer starting fresh takes this rather than 0,
    so it announces what happens next instead of everything that ever did."""
    return conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM sync_events")).scalar() or 0


def trim(conn: Connection, keep: int = DEFAULT_KEEP) -> int:
    """Drop all but the newest `keep` events. Returns how many went.

    A subquery rather than "delete where created_at < x": time is not the
    thing that matters, position is, and a quiet week should not empty the log.
    """
    cutoff = conn.execute(
        text("SELECT id FROM sync_events ORDER BY id DESC LIMIT 1 OFFSET :keep"),
        {"keep": keep},
    ).scalar()
    if cutoff is None:
        return 0
    result = conn.execute(text("DELETE FROM sync_events WHERE id <= :cutoff"),
                          {"cutoff": cutoff})
    return result.rowcount or 0
