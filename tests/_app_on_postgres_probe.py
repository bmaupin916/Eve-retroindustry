"""Start the real application against Postgres and render every page.

**Not collected by pytest** — the leading underscore keeps it out — because it
has to run in a process of its own. `tests/test_app_on_postgres.py` spawns it.

Why a subprocess rather than a fixture: the application binds one database per
*process*. `conftest.py` sets `EVE_APP_DIR` at import so the suite runs on
SQLite, and `app/db/database.py` builds its engine at module import from
whatever `database_url()` said at that moment. Rebinding the environment
afterwards does not move it. So a test that wants the app on Postgres cannot
share a process with a suite that wants it on SQLite, and pretending otherwise
would produce a test that quietly measured SQLite.

What it covers that nothing else does: every route test in the suite runs on
SQLite, and the cross-backend files (`test_pi_sql_on_postgres`,
`test_market_cache_on_postgres`, `test_plan_resolver_on_postgres`) drive
*functions*, not the application. Between them they left one question
unanswered — does the app come up and serve — and the answer was **no** until
v0.9.75: `app/db/database.py` passed `connect_args={"timeout": 30.0}` to psycopg
unconditionally and raised `invalid connection option "timeout"` at import.

The SDE is copied from the committed `sde_base.db` rather than downloaded:
`import_sde.py` fetches a CCP build, and the bundled file is the same data and
already in the repo.

Prints `RESULT ok=<n> bad=<n>` as its last line, and exits non-zero if anything
failed to serve.
"""
from __future__ import annotations

import atexit
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Per-process, not a fixed name. Two probe runs back to back — which is exactly
# what happens when someone reinstates a bug to check this test catches it —
# would otherwise share one schema, and a `DROP SCHEMA ... CASCADE` blocks
# while the previous run still holds a connection to it. That produced one
# unexplained failure immediately after a successful restore, which is the kind
# of flake that gets shrugged off as "just re-run it".
PG_SCHEMA = f"pytest_app_pages_{os.getpid()}"
SDE_FILE = os.path.join(REPO, "sde_base.db")

#: Every page a signed-in owner can reach, plus the API endpoints whose SQL the
#: conversion touched. A page that 500s here is a statement that runs on SQLite
#: and does not run on Postgres.
PAGES = [
    "/", "/assets", "/blueprints", "/planets", "/prices", "/plan",
    "/margins", "/contracts", "/jobs", "/wallet", "/orders",
    "/reactions", "/pi-planner", "/projects", "/settings", "/sync-health",
    "/api/sync-status", "/api/dashboard/pi-alerts",
    "/api/suggest?q=trit", "/api/prices/search?q=tritanium",
]

#: Status codes that count as "the page came back".
#:
#: **404 used to be in here**, and a 404 is not a served page — it is the
#: single most likely symptom of a router that failed to register or a
#: handler that raised on a backend it had never run against. A probe whose
#: healthy result is indistinguishable from a broken one is the failure this
#: project keeps re-finding, and this one cost real time: it is why a
#: five-router Postgres bug looked plausible for an afternoon.
#:
#: 303 and 307 stay because several of these pages legitimately redirect when
#: the SDE is absent, and 401 because the auth gate refusing is a working
#: gate.
SERVED = {"200", "303", "307", "401"}


def _drop_schema() -> None:
    """Remove this process's schema. Safe to call twice — the normal teardown
    calls it and so does `atexit`, and the second one finds nothing."""
    try:
        from sqlalchemy import create_engine, text

        from tests.test_postgres_schema import URL as PG_URL

        admin = create_engine(PG_URL)
        with admin.connect() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
            c.commit()
        admin.dispose()
    except Exception:                       # noqa: BLE001 — best effort teardown
        pass


def _sweep_orphans(c) -> None:
    """Drop `pytest_app_pages_*` schemas left by runs that never tore down.

    Belt as well as braces: `atexit` covers an exception or a clean exit, and
    nothing covers a `SIGKILL`. Sweeping at *setup* is what makes the leak
    self-healing rather than permanent.

    A concurrent run holds its own schema, and `DROP ... CASCADE` would block
    on it rather than fail — so this sets a short `lock_timeout` and moves on.
    Skipping is the right answer there: that schema has a live owner who will
    drop it.
    """
    from sqlalchemy import text

    rows = c.execute(text(
        "SELECT nspname FROM pg_namespace"
        " WHERE nspname LIKE 'pytest\\_app\\_pages%' AND nspname <> :mine"),
        {"mine": PG_SCHEMA}).fetchall()
    for (name,) in rows:
        try:
            c.execute(text("SET LOCAL lock_timeout = '2s'"))
            c.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            c.commit()
            print(f"swept orphaned schema {name}")
        except Exception:                   # noqa: BLE001 — in use by a live run
            c.rollback()


def main() -> int:
    from sqlalchemy import create_engine, text

    from tests.test_postgres_schema import URL as PG_URL, _reachable

    if not _reachable(PG_URL):
        print("SKIP no Postgres reachable")
        return 3

    admin = create_engine(PG_URL)
    with admin.connect() as c:
        _sweep_orphans(c)
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.commit()
    admin.dispose()

    # The teardown at the bottom of this function is the normal path; this is
    # the one that runs when there is no normal path — an exception in the
    # middle, or the process killed outright. Without it a schema holding a
    # full copy of the SDE (22 MB) is orphaned, and because the name carries
    # the pid it can never be reused, so they accumulate. Four had already
    # collected before anyone looked.
    atexit.register(_drop_schema)

    scoped = PG_URL + ("&" if "?" in PG_URL else "?") + \
        f"options=-csearch_path%3D{PG_SCHEMA}"

    # Set before importing anything from `app`: `app/db/database.py` reads the
    # URL at import time and never looks again.
    os.environ["EVE_DATABASE_URL"] = scoped
    os.environ["EVE_APP_DIR"] = tempfile.mkdtemp(prefix="eve-pg-pages-")
    os.environ["EVE_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
    os.environ["EVE_SYNC_WORKER"] = "0"
    os.environ.pop("EVE_OWNER_CHARACTER_ID", None)

    from app.db.location import database_url
    if database_url() != scoped:
        print(f"FAIL the app is bound to {database_url()}, not the probe schema")
        return 2

    from app.db.migrate import upgrade_to_head
    upgrade_to_head()

    from app.db import conn as db_conn
    from app.db.schema import SDE_TABLES, create_sde_schema
    create_sde_schema(db_conn.engine())

    # ── the SDE, copied from the committed bundle ──
    src = sqlite3.connect(f"file:{SDE_FILE}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    eng = db_conn.engine()
    copied = 0
    for table in sorted(SDE_TABLES):
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        if not rows:
            continue
        cols = rows[0].keys()
        stmt = text(f"INSERT INTO {table} ({', '.join(cols)})"
                    f" VALUES ({', '.join(f':{c}' for c in cols)})")
        with eng.connect() as c:
            for i in range(0, len(rows), 5000):
                c.execute(stmt, [dict(r) for r in rows[i:i + 5000]])
            c.commit()
        copied += len(rows)
    src.close()
    if copied < 100_000:
        print(f"FAIL only {copied} SDE rows copied; the pages would degrade for "
              f"lack of data rather than prove anything")
        return 2
    print(f"copied {copied} SDE rows")

    from fastapi.testclient import TestClient

    import app.web.main as m

    bad: list[tuple[str, str]] = []
    ok = 0
    with TestClient(m.app) as client:
        from app.db.conn import connect
        from app.web import security

        with connect() as c:
            c.execute(text(
                "INSERT INTO characters (character_id, character_name,"
                " refresh_token, access_token, token_expires_at,"
                " corporation_id, last_sync_at, added_at)"
                " VALUES (900000001, 'Probe Pilot', 'x', 'x',"
                " 99999999999, 98000001, 0, 0)"))
            for table in ("char_assets_cache", "char_blueprints_cache"):
                c.execute(text(
                    f"INSERT INTO {table} (character_id, data_json, cached_at)"
                    f" VALUES (900000001, '[]', 0)"))
            c.commit()
            security.ensure_sessions_table(c)
            security.claim_owner(c, 900000001)
            sid, csrf = security.create_session(c, 900000001)

        client.cookies.set(security.SESSION_COOKIE, sid)
        client.headers.update({security.CSRF_HEADER: csrf})

        for path in PAGES:
            try:
                r = client.get(path, follow_redirects=False)
                code = str(r.status_code)
                if code in SERVED:
                    ok += 1
                else:
                    bad.append((path, f"{code} {r.text[:200]}"))
            except Exception as exc:            # noqa: BLE001 — this is the probe
                bad.append((path, f"RAISED {type(exc).__name__}: {exc}"))

    db_conn.dispose()
    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.commit()
    admin.dispose()

    for path, why in bad:
        print(f"BAD  {path}: {why}")
    print(f"RESULT ok={ok} bad={len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
