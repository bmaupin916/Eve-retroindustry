"""What the background sync is doing, and whether it is working.

Everything on this page already existed and nothing displayed it. The worker
kept `rounds` and `failures`, the transport kept a quarantine and an ETag
cache, and `app/sync/events.py` wrote an append-only log — and the only readers
of any of it were tests. That gap is not academic: on 2026-08-19 two shipped
defects were found within an hour, and both were caught only because a `[sync]`
line happened to print to a terminal somebody was watching. The reasons were
sitting in `sync_events` the whole time, queryable, with detail.

So this page is deliberately the plainest possible consumer: it reads, it
resolves ids to names, and it renders. No fetching, no fixing, no buttons that
do anything. A health view that can act is a health view you cannot trust to
tell you the truth about the thing it is acting on.

**It is also the event model's first real consumer**, which is half the point of
building it now rather than after five pages depend on the log.

Timestamps are handed to the template raw and formatted by the `age_short`
filter, which already answers "—" for one that never happened. Computing ages
here would be a second opinion about what "5m" means.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth.token_store import list_characters
from app.db.conn import connect
from app.esi.client import etag_stats, quarantine_state
from app.sync import events, worker as sync_worker
from app.web.deps import _tr

router = APIRouter()

#: How many log lines to show. The log holds 20,000; nobody reads 20,000.
EVENT_LIMIT = 60

#: A cache older than this many rounds is called out. Two rather than one: a
#: round takes time and the interval carries ±20% jitter, so one missed round
#: is ordinary and two is a pattern.
STALE_ROUNDS = 2

#: The caches the worker fills, and the label each gets. Keyed by table because
#: a *missing row* is what distinguishes "never synced" from "synced, empty" —
#: the same distinction /jobs makes, and for the same reason.
_CACHES = (
    ("char_assets_cache", "Assets"),
    ("char_blueprints_cache", "Blueprints"),
    ("char_skills_cache", "Skills"),
    ("char_jobs_cache", "Jobs"),
)

#: Kinds that mean something went wrong, so the page can say so without
#: pattern-matching on text. Everything else is ordinary traffic.
_BAD_KINDS = {"sync.character.failed"}


def _character_rows(conn, now: float, stale_after: float) -> list[dict]:
    """One row per character: when it last synced, and how fresh each cache is."""
    chars = list_characters(conn.connection.driver_connection)
    last_sync = dict(conn.exec_driver_sql(
        "SELECT character_id, last_sync_at FROM characters").fetchall())

    freshness: dict[str, dict] = {}
    for table, _label in _CACHES:
        try:
            freshness[table] = dict(conn.exec_driver_sql(
                f"SELECT character_id, cached_at FROM {table}").fetchall())
        except Exception:
            # A cache table that does not exist yet means "never synced" for
            # every character — not a 500 on the page that would have said so.
            freshness[table] = {}

    rows = []
    for char_id, name in chars:
        caches = [{"label": label, "at": freshness[table].get(char_id)}
                  for table, label in _CACHES]
        at = last_sync.get(char_id)
        rows.append({
            "id": char_id,
            "name": name,
            "last_sync_at": at,
            "caches": caches,
            "never": all(c["at"] is None for c in caches),
            "stale": bool(at) and (now - float(at)) > stale_after,
        })
    # Never-synced first: it is the state most likely to need attention, and
    # sorting it last would bury it under characters that are fine.
    rows.sort(key=lambda r: (r["last_sync_at"] is not None, r["last_sync_at"] or 0))
    return rows


@router.get("/sync-health", response_class=HTMLResponse)
async def sync_health_page(request: Request):
    """Read-only. Never calls ESI — which is the whole point of the worker."""
    now = time.time()
    status = sync_worker.status()
    interval = status.get("interval") or sync_worker.DEFAULT_INTERVAL
    stale_after = interval * STALE_ROUNDS

    with connect() as conn:
        try:
            log = events.recent(conn, limit=EVENT_LIMIT)
        except Exception:
            # Before the migration that creates it there is no log at all.
            # Report that rather than failing: "no table" is itself health news.
            log = None
        characters = _character_rows(conn, now, stale_after)

    names = {c["id"]: c["name"] for c in characters}
    entries = None
    if log is not None:
        entries = [{
            "id": e.id,
            "when": e.when,
            "kind": e.kind,
            "who": names.get(e.character_id) or (
                f"character {e.character_id}" if e.character_id else
                f"corporation {e.corporation_id}" if e.corporation_id else ""),
            "detail": e.detail,
            # `reason` is what `_record_failure` writes, and it is the single
            # most useful string on this page — both of the defects found on
            # 2026-08-19 named themselves in it.
            "reason": (e.detail or {}).get("reason", ""),
            "bad": e.kind in _BAD_KINDS,
        } for e in log]

    return _tr("sync_health.html", request, {
        "worker": status,
        "interval_minutes": round(interval / 60),
        "stale_rounds": STALE_ROUNDS,
        "characters": characters,
        "events": entries,
        "failures": [e for e in (entries or []) if e["bad"]][:5],
        "quarantine": sorted(quarantine_state().items()),
        "etags": etag_stats(),
        "event_limit": EVENT_LIMIT,
    })
