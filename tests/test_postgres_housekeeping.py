"""Two pieces of test-harness plumbing that had gone wrong quietly.

**The reachability probe was uncached.** 401 collected tests take a Postgres
fixture and every one of them calls `_reachable`. With the container up that is
free; with it down each call pays a two-second connect timeout, so the run took
**12m27s against 4m21s** — and a suite that takes three times as long looks hung
rather than skipped. This is the inverse of the failure already in
`docs/working-notes.md`, where a *fast* run meant a broken container.

**Nothing dropped the schemas.** Every `test_*_on_postgres.py` drops and
recreates its own `pytest_*` schema at setup and never tears it down, so the
container accumulates one per module — 24 were sitting in the dev container when
this was written. The setup-time drop stays: it is what makes the leak
self-healing after a SIGKILL, which no teardown can cover. This adds the normal
path.

The selector is tested separately from the drop on purpose. A test that called
the real sweep would delete schemas that other modules' fixtures are still
holding, so `_matching_schemas` answers *what would be dropped* without dropping
anything, and only a deliberately narrow pattern is ever actually swept here.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import (
    TEST_SCHEMA_PATTERN,
    _matching_schemas,
    _sweep_postgres_test_schemas,
)
from tests.test_postgres_schema import URL, _reachable

#: A closed port on the loopback: refused immediately, so the probe is fast
#: whether or not it is cached. What is being measured is the call count.
UNREACHABLE = "postgresql+psycopg://nobody@127.0.0.1:1/none"

OURS = "pytest_sweep_probe"
NOT_OURS = "notpytest_sweep_probe"

pg_only = pytest.mark.skipif(
    not _reachable(URL), reason=f"no Postgres at {URL}")


def test_an_unreachable_url_is_probed_once_not_once_per_test():
    """The cache is the fix, so the cache is what gets asserted.

    Three calls, one miss. Uncached this is three connect attempts, and in a
    real run it is 401 of them.
    """
    before = _reachable.cache_info().misses
    assert _reachable(UNREACHABLE) is False
    assert _reachable(UNREACHABLE) is False
    assert _reachable(UNREACHABLE) is False
    assert _reachable.cache_info().misses == before + 1


def test_the_cache_still_answers_the_question_correctly():
    """A cache that always said False would pass the test above and skip the
    entire Postgres suite in silence, which is the worse failure."""
    assert _reachable(UNREACHABLE) is False
    assert _reachable(URL) is _reachable(URL)


def test_a_running_postgres_is_reported_as_reachable():
    """The positive control, and it must not consult `_reachable` to decide.

    Marking this `pg_only` would have made it useless: that marker *is*
    `_reachable`, so a probe that always returned False would skip its own
    positive control and every assertion in this file would still pass while
    the entire Postgres suite silently stopped running.
    """
    engine = create_engine(URL, connect_args={"connect_timeout": 2})
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        pytest.skip(f"no Postgres at {URL}")
    finally:
        engine.dispose()

    assert _reachable(URL) is True


@pg_only
def test_the_pattern_matches_our_schemas_and_leaves_others_alone():
    """The safety assertion: `notpytest_...` must survive the sweep.

    Asserted through the selector rather than by running the sweep, because
    running it would drop schemas other modules are still using.
    """
    engine = create_engine(URL)
    try:
        with engine.connect() as conn:
            for name in (OURS, NOT_OURS):
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{name}"'))
            conn.commit()

            matched = _matching_schemas(conn, TEST_SCHEMA_PATTERN)
            assert OURS in matched
            assert NOT_OURS not in matched

            for name in (OURS, NOT_OURS):
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            conn.commit()
    finally:
        engine.dispose()


@pg_only
def test_the_sweep_drops_what_the_selector_matched():
    """The other half — the selector could be right and the drop a no-op.

    Swept with a pattern narrow enough to hit only this test's own schema.
    """
    engine = create_engine(URL)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{OURS}"'))
            conn.commit()
            assert OURS in _matching_schemas(conn, TEST_SCHEMA_PATTERN)

        assert _sweep_postgres_test_schemas("pytest\\_sweep\\_probe") == [OURS]

        with engine.connect() as conn:
            assert OURS not in _matching_schemas(conn, TEST_SCHEMA_PATTERN)
    finally:
        with engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{OURS}" CASCADE'))
            conn.commit()
        engine.dispose()


@pg_only
def test_the_underscore_in_the_pattern_is_escaped():
    """In LIKE, a bare `_` is a single-character wildcard.

    `pytest_%` unescaped also matches `pytestXfoo`. Nothing is named that
    today — which is precisely why the day something is, nobody would connect
    the vanished schema to this pattern.
    """
    engine = create_engine(URL)
    decoy = "pytestXsweep_probe"
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{decoy}"'))
            conn.commit()
            assert decoy not in _matching_schemas(conn, TEST_SCHEMA_PATTERN)
            # ... and it *would* have matched without the escape.
            assert decoy in _matching_schemas(conn, "pytest_%")
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{decoy}" CASCADE'))
            conn.commit()
    finally:
        engine.dispose()
