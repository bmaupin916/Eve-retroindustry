"""The event log the Discord bot will read.

§9.5 says the sync worker has to emit events rather than only refresh caches,
because a bot that polls a cache for changes misses them — two changes between
polls look like one, a change that reverts looks like none — and that
retrofitting this is more work than building it in.

The properties that make it usable by a second process across a restart are the
ones worth pinning: a cursor that cannot skip or repeat, ordering by id rather
than by time, and a trim that bounds the log by position rather than by age.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import conn as db
from app.db.schema import apply_schema
from app.sync import events


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("EVE_DATABASE_URL", f"sqlite:///{tmp_path / 'e.db'}")
    db.dispose()
    with db.connect() as c:
        apply_schema(c.connection.driver_connection)
        yield c
    db.dispose()


def test_an_event_comes_back_the_way_it_went_in(conn):
    events.emit(conn, "job.completed", character_id=95123456,
                detail={"job_id": 7, "product": "Megathron"})
    conn.commit()

    got = events.since(conn)
    assert len(got) == 1
    e = got[0]
    assert e.kind == "job.completed"
    assert e.character_id == 95123456
    assert e.corporation_id is None
    assert e.detail == {"job_id": 7, "product": "Megathron"}
    assert e.created_at > 0


def test_a_cursor_neither_repeats_nor_skips(conn):
    """The whole point of the id: a consumer that stores where it got to can
    come back after a restart and be told exactly what it missed."""
    for i in range(5):
        events.emit(conn, "character.assets.changed", character_id=1,
                    detail={"n": i})
    conn.commit()

    first = events.since(conn, 0, limit=2)
    assert [e.detail["n"] for e in first] == [0, 1]

    cursor = first[-1].id
    rest = events.since(conn, cursor)
    assert [e.detail["n"] for e in rest] == [2, 3, 4]
    assert not any(e.id <= cursor for e in rest)

    # Nothing new: the cursor holds.
    assert events.since(conn, rest[-1].id) == []


def test_ordering_is_by_id_not_by_time(conn):
    """Several events in the same second are normal — one sync writes a handful
    — and a clock can go backwards over NTP or a container restart.

    The two orderings are made to disagree here on purpose: timestamps descend
    while ids ascend. Emitting them in the same second is not enough to tell the
    orderings apart, because SQLite happens to return equal keys in insertion
    order, and a test that cannot distinguish the two proves nothing.
    """
    for i in range(4):
        events.emit(conn, "character.wallet.changed", character_id=1,
                    detail={"n": i}, now=1_800_000_000 - i)
    conn.commit()

    got = events.since(conn)
    assert [e.detail["n"] for e in got] == [0, 1, 2, 3], (
        "events came back in timestamp order; a consumer advancing its cursor "
        "past the highest id it saw would then skip the rest"
    )
    assert [e.id for e in got] == sorted(e.id for e in got)

    # And a cursor taken mid-way still returns exactly the remainder.
    assert [e.detail["n"] for e in events.since(conn, got[1].id)] == [2, 3]


def test_a_fresh_consumer_can_start_from_now(conn):
    """A bot joining an alliance should announce what happens next, not replay
    every asset change since the app was installed."""
    for i in range(3):
        events.emit(conn, "character.assets.changed", character_id=1, detail={"n": i})
    conn.commit()

    start = events.latest_id(conn)
    assert events.since(conn, start) == []

    events.emit(conn, "job.completed", character_id=1, detail={"job_id": 9})
    conn.commit()
    fresh = events.since(conn, start)
    assert [e.kind for e in fresh] == ["job.completed"]


def test_latest_id_is_zero_on_an_empty_log(conn):
    assert events.latest_id(conn) == 0


def test_a_consumer_can_ask_for_only_the_kinds_it_cares_about(conn):
    events.emit(conn, "character.assets.changed", character_id=1)
    events.emit(conn, "job.completed", character_id=1)
    events.emit(conn, "character.wallet.changed", character_id=1)
    events.emit(conn, "job.started", character_id=1)
    conn.commit()

    got = events.since(conn, kinds={"job.completed", "job.started"})
    assert [e.kind for e in got] == ["job.completed", "job.started"]


def test_an_unknown_kind_is_refused(conn):
    """A kind that exists only at the call site is a subscriber that silently
    never fires — the exact failure this log is meant to prevent."""
    with pytest.raises(ValueError, match="unknown event kind"):
        events.emit(conn, "job.finished")          # the real one is job.completed


def test_emit_does_not_commit_on_its_own(conn):
    """An event announcing a cache update has to land in the same commit as the
    update, or a consumer is told about a change it cannot yet read."""
    events.emit(conn, "job.completed", character_id=1)
    conn.rollback()
    assert events.since(conn) == []


def test_trim_keeps_the_newest_and_bounds_the_log(conn):
    for i in range(50):
        events.emit(conn, "character.assets.changed", character_id=1, detail={"n": i})
    conn.commit()

    removed = events.trim(conn, keep=10)
    conn.commit()

    assert removed == 40
    left = events.since(conn)
    assert len(left) == 10
    assert [e.detail["n"] for e in left] == list(range(40, 50))


def test_trim_bounds_by_position_not_by_age(conn):
    """A quiet week must not empty the log — the events that are left are the
    ones a consumer has not read, whenever they happened."""
    for i in range(5):
        events.emit(conn, "job.completed", character_id=1, detail={"n": i},
                    now=1_000_000_000)          # all of them ancient
    conn.commit()

    assert events.trim(conn, keep=10) == 0
    assert len(events.since(conn)) == 5


def test_trim_on_an_empty_log_does_nothing(conn):
    assert events.trim(conn, keep=10) == 0


def test_the_detail_is_a_description_not_a_copy_of_the_payload(conn):
    """Documented as a rule, asserted as a size.

    An event carrying the new asset list is a second answer that can disagree
    with the cache, which is the authoritative one. Keeping details small is
    what stops that happening by accident.
    """
    events.emit(conn, "character.assets.changed", character_id=1,
                detail={"added": 3, "removed": 1})
    conn.commit()

    stored = conn.execute(text("SELECT detail_json FROM sync_events")).scalar()
    assert len(stored) < 200, (
        "an event detail this large is probably a copy of the payload; the "
        "cache is where the new value lives"
    )


def test_corporation_events_carry_the_corporation(conn):
    events.emit(conn, "corporation.assets.changed", corporation_id=98000001,
                detail={"division": 1})
    conn.commit()

    e = events.since(conn)[0]
    assert e.corporation_id == 98000001
    assert e.character_id is None


def test_the_table_is_in_the_migration_history():
    """The schema declaration and the Alembic history must agree — a table that
    exists only in the declaration never reaches a fresh Postgres."""
    from app.db.schema import APP_TABLES

    assert "sync_events" in APP_TABLES
    history = (
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "migrations" / "versions"
    )
    assert any("sync_events" in p.read_text(encoding="utf-8")
               for p in history.glob("*.py")), (
        "sync_events is declared but no migration creates it"
    )


def test_recent_answers_a_different_question_from_since(conn):
    """`since` is for a consumer working forwards from a cursor; `recent` is for
    a human asking what just happened. Using `since(0, limit=N)` for the second
    returns the *oldest* N, which on a busy log means the page can never show
    the line that matters."""
    for n in range(10):
        events.emit(conn, "character.assets.changed", character_id=1, detail={"n": n})
    conn.commit()

    newest = events.recent(conn, limit=3)
    oldest = events.since(conn, 0, limit=3)

    assert [e.detail["n"] for e in newest] == [9, 8, 7], "not newest-first"
    assert [e.detail["n"] for e in oldest] == [0, 1, 2], "since() changed meaning"


def test_recent_survives_the_gaps_trim_leaves(conn):
    """Why `recent` is a query and not `latest_id() - limit`: trim deletes by
    position, so ids have holes and that arithmetic silently returns short."""
    for n in range(10):
        events.emit(conn, "character.assets.changed", character_id=1, detail={"n": n})
    conn.commit()
    events.trim(conn, keep=4)
    conn.commit()

    got = events.recent(conn, limit=4)

    assert len(got) == 4, f"asked for 4 after a trim, got {len(got)}"
    assert [e.detail["n"] for e in got] == [9, 8, 7, 6]
