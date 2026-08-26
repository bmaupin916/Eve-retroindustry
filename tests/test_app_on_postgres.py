"""The application itself, served from Postgres.

Everything else in this suite tests the app on SQLite. The cross-backend files
— `test_pi_sql_on_postgres`, `test_market_cache_on_postgres`,
`test_plan_resolver_on_postgres`, `test_postgres_schema` — run real SQL against
a real Postgres, but they drive **functions**. None of them starts the app.

That gap had a bug in it. Until v0.9.75 `app/db/database.py` built its engine
with `connect_args={"timeout": 30.0}` unconditionally — a `sqlite3.connect`
argument — and called `create_all` at *import* time. On Postgres that raised
`ProgrammingError: invalid connection option "timeout"` before any application
code ran, and since `app/web/main.py` imports `get_session` from that module,
**the app could not start at all**. Fifteen hundred passing tests could not see
it, because every one of them binds SQLite.

So this file exists to ask the one question the rest of the suite structurally
cannot: does the thing come up and serve.

**It runs the app in a subprocess, and that is not laziness.** The application
binds one database per *process*: `conftest.py` sets `EVE_APP_DIR` at import so
the suite runs on SQLite, and `app/db/database.py` fixes its engine at module
import from whatever `database_url()` said then. Rebinding the environment
afterwards does not move it. A test wanting the app on Postgres therefore cannot
share a process with a suite wanting it on SQLite — and one that tried would
quietly measure SQLite and pass.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "tests" / "_app_on_postgres_probe.py"


@pytest.fixture(scope="module")
def probe_run() -> subprocess.CompletedProcess:
    """Run the probe once; every assertion below reads the same result."""
    assert PROBE.is_file(), f"the probe script is missing: {PROBE}"
    return subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600,
    )


def _tail(stream: str | None, n: int = 3000) -> str:
    """The end of a captured stream, tolerating `None`.

    Not defensive programming for its own sake. The first time this test caught
    the bug it exists for, the failure surfaced as
    `TypeError: 'NoneType' object is not subscriptable` raised from inside the
    assertion *message* — so the output said nothing whatsoever about the app
    having failed to start on Postgres, which is the one thing a reader needs.
    A test that detects the right problem and reports the wrong one has done
    half its job.
    """
    return (stream or "")[-n:]


def _result(run: subprocess.CompletedProcess) -> tuple[int, int]:
    """`(ok, bad)` from the probe's last line."""
    line = next((l for l in (run.stdout or "").splitlines()
                 if l.startswith("RESULT ")), None)
    assert line, (
        f"the probe exited {run.returncode} without printing a RESULT line, so "
        f"it did not reach the end — most often because the app failed to "
        f"start:\n--- stdout ---\n{_tail(run.stdout)}\n"
        f"--- stderr ---\n{_tail(run.stderr)}")
    parts = dict(p.split("=") for p in line.split()[1:])
    return int(parts["ok"]), int(parts["bad"])


def test_every_page_serves_on_postgres(probe_run):
    """The whole point. A 500 here is a statement that runs on SQLite and does
    not run on Postgres — which is the failure this conversion existed to
    prevent, and the only place in the suite it can show up."""
    if probe_run.returncode == 3:
        pytest.skip("no Postgres reachable — see tests/test_postgres_schema.py")

    ok, bad = _result(probe_run)
    assert bad == 0, (
        "pages failed to serve on Postgres:\n" + _tail(probe_run.stdout, 4000))
    assert ok >= 15, (
        f"only {ok} pages were served, which is too few to mean anything — the "
        f"probe's page list has probably shrunk")


def test_the_probe_actually_loaded_the_static_data(probe_run):
    """A positive control.

    Without the SDE every page still answers — it degrades to an empty state or
    the setup gate — so "20/20 served" against an empty database would prove
    almost nothing. The probe refuses to continue below 100,000 rows; this
    asserts it said so, because a control that is only checked inside the thing
    it controls is not a control.
    """
    if probe_run.returncode == 3:
        pytest.skip("no Postgres reachable")

    copied = next((l for l in probe_run.stdout.splitlines()
                   if l.startswith("copied ")), None)
    assert copied, f"the probe did not report copying the SDE:\n{_tail(probe_run.stdout, 2000)}"
    n = int(copied.split()[1])
    assert n >= 100_000, f"only {n} SDE rows reached Postgres"


def test_the_probe_binds_postgres_and_not_sqlite(probe_run):
    """The measurement this file most needs to be right about.

    A probe that silently ran on SQLite would pass every assertion above and
    mean nothing — the exact shape of failure this codebase keeps finding in its
    own selectors. The probe checks `database_url()` against the schema it
    created and exits 2 if they disagree; anything other than 0 or 3 here means
    that check fired.
    """
    assert probe_run.returncode in (0, 3), (
        f"the probe exited {probe_run.returncode}:\n"
        + _tail(probe_run.stdout) + "\n" + _tail(probe_run.stderr, 2000))
