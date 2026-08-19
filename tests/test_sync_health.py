"""The sync log's first real consumer, and the page that reads it.

Before this page, `worker.status()`, `quarantine_state()`, `etag_stats()` and
the whole `sync_events` table had no caller outside a test — the worker wrote
events nothing read. Two shipped defects were found on 2026-08-19 only because
a `[sync]` line printed to a terminal somebody happened to be watching, while
the reasons sat in the log the whole time.

So the assertions here are mostly about *what the page says*, not that it
returns 200. A health page that renders is not the same as a health page that
reports, and the second is the only one worth having: this is precisely the
class of test this repo keeps catching itself writing badly — a name that
claims something about the world needs an assertion about the world.
"""
from __future__ import annotations

import json
import time

import pytest

from app.sync import events
from app.web.routers import sync_health as health


@pytest.fixture
def log(app_module):
    """Write events straight into the table the page reads."""
    from app.db import conn as db

    def _write(rows):
        with db.connect() as c:
            c.execute(events.text("DELETE FROM sync_events"))
            for kind, char_id, detail in rows:
                events.emit(c, kind, character_id=char_id, detail=detail)
            c.commit()

    with db.connect() as c:
        c.execute(events.text("DELETE FROM sync_events"))
        c.commit()
    return _write


# ── it renders at all ────────────────────────────────────────────────────────

def test_the_page_renders(client):
    r = client.get("/sync-health")
    assert r.status_code == 200
    assert "Sync Health" in r.text


def test_the_page_never_calls_esi(client, monkeypatch):
    """The point of the worker is that pages do not fetch. A health page that
    fetched to report on fetching would be the joke version of this."""
    from app.esi import client as esi

    def _boom(*a, **kw):
        raise AssertionError("the sync-health page called ESI")

    monkeypatch.setattr(esi, "esi_client", _boom)
    assert client.get("/sync-health").status_code == 200


# ── the log ──────────────────────────────────────────────────────────────────

def test_a_failure_reason_reaches_the_page(client, log):
    """The single most valuable string on the page. Both defects found on
    2026-08-19 named themselves in `detail.reason`, and neither was displayed
    anywhere — this is the assertion that says it is now."""
    log([("sync.character.failed", 900000001,
          {"reason": "Error -3 while decompressing data: incorrect header check"})])

    page = client.get("/sync-health").text

    assert "incorrect header check" in page, (
        "the failure reason is in the log but not on the page, which is the "
        "state this page exists to end")


def test_a_failure_is_marked_as_one(client, log):
    """`sync.character.failed` has to read differently from ordinary traffic,
    or a wall of grey rows hides the one line that matters."""
    log([("character.assets.changed", 900000001, {"added": 2}),
         ("sync.character.failed", 900000001, {"reason": "no valid token"})])

    page = client.get("/sync-health").text

    assert "Recent sync failures" in page, "a failure was not called out"
    assert "no valid token" in page


def test_ordinary_events_do_not_raise_the_alarm(client, log):
    """The other half of the previous test: if everything is flagged, nothing
    is. A page that shouts on a normal round gets ignored on a bad one."""
    log([("character.assets.changed", 900000001, {"added": 2}),
         ("character.skills.changed", 900000001, {})])

    page = client.get("/sync-health").text

    assert "Recent sync failures" not in page, "an ordinary round was reported as a failure"
    assert "character.assets.changed" in page, "the event is missing entirely"


def test_an_empty_log_says_why_it_is_empty(client, log):
    """An empty log is the *expected* state after a first sync, because first
    sight is deliberately never a change. Silence there looks like breakage."""
    log([])

    page = client.get("/sync-health").text

    assert "Nothing logged yet" in page
    assert "first sight" in page.lower(), (
        "an empty log is shown without explaining that a first sync is silent "
        "by design — which reads as a broken worker")


def test_the_newest_events_are_the_ones_shown(client, log, monkeypatch):
    """`recent()` and `since()` answer different questions. Showing the oldest
    N would put a page's most useful line permanently out of reach."""
    monkeypatch.setattr(health, "EVENT_LIMIT", 3)
    log([("character.assets.changed", 900000001, {"n": n}) for n in range(6)])

    page = client.get("/sync-health").text

    assert "n=5" in page, "the newest event is not shown"
    assert "n=0" not in page, "the page is showing the oldest events, not the newest"


# ── characters ───────────────────────────────────────────────────────────────

def test_a_character_the_worker_has_not_reached_says_so(client, app_module):
    """"Never synced" and "synced, nothing there" are different answers, and
    conflating them is the same bug /jobs was converted to avoid."""
    from app.db import conn as db

    with db.connect() as c:
        for table, _label in health._CACHES:
            c.exec_driver_sql(f"DELETE FROM {table}")
        c.commit()

    page = client.get("/sync-health").text

    assert "never synced" in page, (
        "a character with no cache row at all was not reported as unsynced")


def test_a_freshly_synced_character_is_not_flagged(client, app_module):
    """The control for the test above. If everything reads 'never synced' the
    badge means nothing."""
    from app.db import conn as db

    now = time.time()
    with db.connect() as c:
        for table, _label in health._CACHES:
            c.exec_driver_sql(f"DELETE FROM {table}")
        c.exec_driver_sql(
            "INSERT INTO char_assets_cache (character_id, data_json, cached_at)"
            f" VALUES (900000001, '{json.dumps([])}', {now})")
        c.exec_driver_sql(
            "INSERT INTO char_jobs_cache (character_id, data_json, cached_at)"
            f" VALUES (900000001, '{json.dumps([])}', {now})")
        c.exec_driver_sql(
            "INSERT INTO char_blueprints_cache (character_id, data_json, cached_at)"
            f" VALUES (900000001, '{json.dumps([])}', {now})")
        c.exec_driver_sql(
            "INSERT INTO char_skills_cache (character_id, data_json, cached_at)"
            f" VALUES (900000001, '{json.dumps({})}', {now})")
        c.commit()

    rows = _rows_for(client)
    fresh = [r for r in rows if not r["never"]]
    assert fresh, "a character synced one second ago was reported as never synced"


def _rows_for(client):
    """The view model rather than the HTML, for the assertions that are about
    the data and would otherwise be a substring search against markup."""
    import app.web.routers.sync_health as mod
    captured = {}
    real = mod._tr

    def _spy(name, request, context):
        captured.update(context)
        return real(name, request, context)

    mod._tr = _spy
    try:
        client.get("/sync-health")
    finally:
        mod._tr = real
    return captured["characters"]


# ── the transport's own state ────────────────────────────────────────────────

def test_a_quarantined_entity_is_named(client, monkeypatch):
    """Quarantine is invisible by design — the transport answers locally and
    the caller sees a normal refusal. That is exactly why it needs a display."""
    monkeypatch.setattr(health, "quarantine_state",
                        lambda: {"characters/95123456": 1800.0})

    page = client.get("/sync-health").text

    assert "characters/95123456" in page
    assert "Held back by the transport" in page


def test_nothing_quarantined_reads_as_healthy(client, monkeypatch):
    monkeypatch.setattr(health, "quarantine_state", lambda: {})

    page = client.get("/sync-health").text

    assert "Held back by the transport" not in page
    assert "every token is answering" in page


def test_the_etag_cache_is_reported(client, monkeypatch):
    monkeypatch.setattr(health, "etag_stats",
                        lambda: {"entries": 12, "bytes": 2 * 1024 * 1024,
                                 "hits": 47, "misses": 3})

    page = client.get("/sync-health").text

    assert "47" in page, "the hit count is not shown"
    assert "12 cached" in page


# ── the worker ───────────────────────────────────────────────────────────────

def test_a_stopped_worker_is_not_reported_as_running(client, monkeypatch):
    """`EVE_SYNC_WORKER=0` is a normal state and reads differently from a
    worker that should be running and is not — the second is a problem."""
    monkeypatch.setattr(health.sync_worker, "status",
                        lambda: {"running": False, "enabled": False})
    assert "Disabled" in client.get("/sync-health").text

    monkeypatch.setattr(health.sync_worker, "status",
                        lambda: {"running": False, "enabled": True})
    page = client.get("/sync-health").text
    assert "Not running" in page, (
        "a worker that is enabled but dead was not distinguished from one that "
        "was switched off on purpose")
