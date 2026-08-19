"""W11: SQLite under a continuously writing sync worker.

The worry, stated plainly: the background worker in `app/sync/worker.py` now
writes to `eve_cache.db` on a loop, forever, while the web layer reads and
writes the same file to serve pages. Before tonight nothing wrote continuously,
so "does SQLite hold up" was a question nobody had to answer.

**Two connection layers share one file, on purpose.** The worker writes through
the SQLAlchemy engine (`app.db.conn.connect`); the eleven routers read and write
through the raw `sqlite3` handle (`app.web.deps.get_conn`). The query conversion
that will collapse these into one is deliberately not atomic — 1 of 11 modules
so far — so they coexist for as long as that takes. That is the configuration
this file has to vouch for, not some future tidier one.

**Why WAL is the whole answer.** In the default rollback-journal mode a writer
blocks every reader, so a worker mid-write makes pages fail rather than wait.
WAL lets any number of readers run against the last committed snapshot while one
writer works. Writers still serialise against each other — WAL does not change
that — which is what `busy_timeout` is for: the second writer waits instead of
raising. Both settings are already applied at every path that writes. Nothing
asserted them on the raw path, and nothing had ever run the two layers at once.

**What this file does not claim.** It vouches for one user's traffic against a
local file. It says nothing about many concurrent users, which is Step 5's
problem and which Postgres, not a pragma, is the answer to. W11 is retired at
the scope W11 was written at: the worker must not break the app.
"""
from __future__ import annotations

import ast
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import text

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app"

#: Sites that call `sqlite3.connect` themselves *and* apply the pragmas.
#: Everything else in `app/` must be provably read-only — see the scan for why.
#: `app/db/conn.py` is deliberately absent: it never calls `sqlite3.connect`,
#: SQLAlchemy does, and its pragmas hang off a connect event that
#: `test_db_conn.py::test_sqlite_still_gets_its_pragmas` covers instead.
PRAGMA_SITES = {
    "app/web/deps.py",         # the raw handle every router opens
    "app/auth/esi_oauth.py",   # rotating refresh tokens, a genuine writer
    "app/web/bootstrap.py",    # first-run setup, before anything else exists
}

WRITE_SQL = ("insert into", "update ", "delete from", "create table",
             "drop table", "alter table", "replace into")


@pytest.fixture
def solo_db(tmp_path, monkeypatch):
    """A database file of this test's own, reachable by both layers.

    Both `database_path()` and `database_url()` derive from `EVE_APP_DIR`, so
    pointing that at a fresh directory aims the engine and the raw handle at the
    same new file — which is exactly the arrangement under test.
    """
    from app.db import conn as engine_layer
    from app.db import schema

    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    monkeypatch.delenv("EVE_DATABASE_URL", raising=False)
    engine_layer.dispose()
    schema.forget_applied()

    from app.web.deps import get_conn
    boot = get_conn()          # creates the file and applies the app schema
    boot.close()

    yield tmp_path

    engine_layer.dispose()
    schema.forget_applied()


# ── The pragmas, on the path the app actually uses ───────────────────────────

def test_the_raw_handle_every_router_opens_is_in_wal(solo_db):
    """`test_db_conn.py` asserts this for the engine. Eleven routers do not use
    the engine — they call `get_conn()` — so the assertion has to exist twice."""
    from app.web.deps import get_conn

    conn = get_conn()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()

    assert mode.lower() == "wal", (
        "the routers' connection is in rollback-journal mode, where the "
        "worker's writes block every page read")
    assert busy == 30000, "no busy timeout: a writer-writer wait raises instead of waiting"


def test_both_layers_agree_about_the_journal(solo_db):
    """journal_mode is a property of the file, not the connection. If the two
    layers disagreed, the last one to open would silently retune the other."""
    from app.db import conn as engine_layer
    from app.web.deps import get_conn

    raw = get_conn()
    try:
        raw_mode = raw.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        raw.close()
    with engine_layer.connect() as c:
        eng_mode = c.execute(text("PRAGMA journal_mode")).fetchone()[0].lower()

    assert raw_mode == eng_mode == "wal"


# ── No third connection site may write ───────────────────────────────────────

def _connect_sites() -> dict[str, bool]:
    """Every `sqlite3.connect` under `app/`, mapped to whether its module writes.

    A module that only reads is safe at any pragma setting: under WAL a reader
    never blocks and is never blocked. A module that writes without a busy
    timeout is the one that raises "database is locked" the first time the
    worker happens to be mid-commit.
    """
    sites: dict[str, bool] = {}
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "sqlite3.connect" not in src:
            continue
        rel = path.relative_to(REPO).as_posix()
        lowered = " ".join(
            n.value.lower() for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        sites[rel] = any(w in lowered for w in WRITE_SQL)
    return sites


def test_the_scan_sees_the_connection_sites_it_is_meant_to(solo_db):
    """A positive control. This scan's healthy result is "nothing new", which is
    indistinguishable from a scan that has stopped reading the tree at all."""
    sites = _connect_sites()
    assert sites, "the scan found no sqlite3.connect anywhere — it is broken"
    assert "app/web/deps.py" in sites, "the scan missed the routers' own handle"
    assert sites["app/web/deps.py"] is True, (
        "the scan cannot tell a writing module from a reading one")


def test_no_unguarded_connection_writes(solo_db):
    offenders = sorted(
        rel for rel, writes in _connect_sites().items()
        if writes and rel not in PRAGMA_SITES
    )
    assert not offenders, (
        "these open SQLite directly, write, and never set busy_timeout:\n  "
        + "\n  ".join(offenders)
        + "\nThey will raise 'database is locked' whenever the sync worker is "
          "mid-commit. Route them through app.db.conn.connect(), or add the "
          "pragmas and list the file in PRAGMA_SITES with a reason.")


def test_the_exemption_list_is_all_still_real(solo_db):
    """A PRAGMA_SITES entry for a file that no longer opens SQLite is a licence
    nobody revoked, and hides the day a real one goes missing."""
    sites = set(_connect_sites())
    stale = sorted(PRAGMA_SITES - sites)
    assert not stale, f"listed but no longer connect: {stale}"


# ── The claim itself: a writer does not take the app down ────────────────────

def _hold_write_lock(path: str, started: threading.Event,
                     release: threading.Event, errors: list) -> None:
    """Occupy the write lock the way a worker commit does, but for longer."""
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO sync_events"
                     " (created_at, kind, detail_json) VALUES (?,?,?)",
                     (time.time(), "character.added", "{}"))
        started.set()
        release.wait(timeout=10)
        conn.commit()
    except Exception as exc:            # pragma: no cover - reported, not raised
        errors.append(exc)
        started.set()
    finally:
        conn.close()


def _hold_read(path: str, started: threading.Event,
               release: threading.Event, errors: list) -> None:
    """A page mid-render: a transaction open across several reads."""
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()
        started.set()
        release.wait(timeout=20)
        conn.rollback()
    except Exception as exc:            # pragma: no cover - reported, not raised
        errors.append(exc)
        started.set()
    finally:
        conn.close()


def test_the_worker_can_commit_while_a_page_holds_a_read(solo_db):
    """The discriminating case, and the one W11 is actually about.

    An earlier version of this test had the *writer* hold and timed a read, on
    the theory that a write transaction blocks readers. It does not: SQLite only
    escalates to EXCLUSIVE at commit, so that test passed in rollback-journal
    mode too — it asserted nothing. This is the direction that differs. A reader
    holds SHARED for the life of its transaction, and in rollback-journal mode a
    writer cannot commit until every reader has let go. That is the worker
    stalling behind a page, forever, on a site where pages are always rendering.
    Under WAL the writer appends and commits regardless.
    """
    from app.db import conn as engine_layer
    from app.sync import events
    from app.db.location import database_path

    started, release, errors = threading.Event(), threading.Event(), []
    t = threading.Thread(target=_hold_read,
                         args=(database_path(), started, release, errors))
    t.start()
    try:
        assert started.wait(timeout=5), "the reader never opened its transaction"
        began = time.monotonic()
        with engine_layer.connect() as c:
            events.emit(c, "character.wallet.changed", character_id=7)
            c.commit()
        took = time.monotonic() - began
    finally:
        release.set()
        t.join(timeout=25)

    assert not errors, f"the reader itself failed: {errors}"
    assert took < 2.0, (
        f"the worker waited {took:.1f}s to commit behind one open read. That is "
        "rollback-journal behaviour: with pages rendering continuously the sync "
        "worker would stall indefinitely.")


def test_a_second_writer_waits_instead_of_raising(solo_db):
    """WAL does not let two writers run at once, and was never going to. The
    busy timeout is what turns the collision into a pause instead of a 500."""
    from app.db import conn as engine_layer
    from app.sync import events
    from app.db.location import database_path

    started, release, errors = threading.Event(), threading.Event(), []
    t = threading.Thread(target=_hold_write_lock,
                         args=(database_path(), started, release, errors))
    t.start()
    try:
        assert started.wait(timeout=5)

        done = threading.Event()

        def second_writer():
            try:
                with engine_layer.connect() as c:
                    events.emit(c, "character.skills.changed", character_id=1)
                    c.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        w = threading.Thread(target=second_writer)
        w.start()
        # Still blocked while the first transaction is open: that is correct.
        assert not done.wait(timeout=0.5), "the second write did not serialise at all"
        release.set()
        assert done.wait(timeout=15), "the second writer never finished"
        w.join(timeout=5)
    finally:
        release.set()
        t.join(timeout=10)

    assert not errors, (
        "a second writer raised instead of waiting out the first:\n  "
        + "\n  ".join(f"{type(e).__name__}: {e}" for e in errors))


def test_the_lock_detector_actually_detects_a_lock(solo_db):
    """The positive control for the two tests above.

    Both of them pass by *not* seeing an error, which is also what they would do
    if the threads never overlapped or the file were never locked. So: same
    shape, pragmas removed, and it must fail. If this test stops failing-as-
    expected, the two above have stopped proving anything.
    """
    from app.db.location import database_path

    path = database_path()
    started, release, errors = threading.Event(), threading.Event(), []
    t = threading.Thread(target=_hold_write_lock,
                         args=(path, started, release, errors))
    t.start()
    try:
        assert started.wait(timeout=5)
        naive = sqlite3.connect(path, timeout=0)
        try:
            naive.execute("PRAGMA busy_timeout=0")     # the setting under test
            with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
                naive.execute("INSERT INTO sync_events"
                              " (created_at, kind, detail_json) VALUES (?,?,?)",
                              (time.time(), "job.started", "{}"))
                naive.commit()
        finally:
            naive.close()
    finally:
        release.set()
        t.join(timeout=10)


def test_both_layers_writing_at_once_lose_nothing(solo_db):
    """The soak. Worker-shaped writes through the engine and page-shaped writes
    through the raw handle, interleaved, and every row has to arrive."""
    from app.db import conn as engine_layer
    from app.sync import events
    from app.web.deps import get_conn

    ROUNDS = 25
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker_side():
        barrier.wait()
        for _ in range(ROUNDS):
            try:
                with engine_layer.connect() as c:
                    events.emit(c, "character.assets.changed", character_id=42)
                    c.commit()
            except Exception as exc:
                errors.append(exc)

    def page_side():
        barrier.wait()
        for _ in range(ROUNDS):
            try:
                conn = get_conn()
                try:
                    conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()
                    conn.execute("INSERT INTO sync_events"
                                 " (created_at, kind, detail_json) VALUES (?,?,?)",
                                 (time.time(), "job.completed", "{}"))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker_side),
               threading.Thread(target=page_side)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)

    assert not errors, (
        f"{len(errors)} failures with both layers writing:\n  "
        + "\n  ".join(f"{type(e).__name__}: {e}" for e in errors[:5]))

    conn = get_conn()
    try:
        landed = conn.execute(
            "SELECT COUNT(*) FROM sync_events WHERE kind IN"
            " ('character.assets.changed','job.completed')").fetchone()[0]
    finally:
        conn.close()
    assert landed == ROUNDS * 2, (
        f"{ROUNDS * 2} rows were committed but {landed} arrived — writes are "
        "being lost, which a busy timeout would have turned into an error")
