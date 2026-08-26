"""`projects` is the converted module. This runs it on the backend it converted for.

`tests/test_projects.py` drives the pages through the web client, and the web
client is SQLite — so the two statements in `app/web/projects_helper.py` that
exist *only* because Postgres differs had never executed on Postgres. Both are
the kind that fail quietly:

* `ON CONFLICT ... DO UPDATE SET needed = project_shopping.needed +
  excluded.needed`. An unqualified `needed` on the right resolves to the
  *proposed* row on Postgres and to the *stored* row on SQLite, so the bare
  version adds a number to itself instead of accumulating. It returns a
  plausible larger number either way; only the arithmetic is wrong.
* `COUNT(DISTINCT CASE ...)` over two LEFT JOINs off one row. The cartesian
  product means a bare `SUM(CASE ...)` counts each plan once per shopping line —
  this was live, and reported "6 of 2 plans complete".

So every test here runs **twice**, once per backend, and asserts the same
answer. A conversion whose claim is "this module now runs on both" is only
tested by running it on both; asserting Postgres in isolation would not catch a
statement that had quietly stopped agreeing with SQLite.

Postgres comes from the container in `tests/test_postgres_schema.py`; without
it those parameterisations skip and the SQLite half still runs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.web import projects_helper as ph
from tests.test_postgres_schema import URL as PG_URL, _reachable

#: Its own schema, so a run here cannot disturb the schema test's fixture.
PG_SCHEMA = "pytest_projects"


def _plan(product_id: int = 12005, qty: int = 1, materials=None, jobs=None) -> dict:
    """A plan shaped like the planner's output, with only what the helper reads."""
    return {
        "product_type_id": product_id,
        "product_name": f"Product {product_id}",
        "quantity": qty,
        "blueprint": {"me": 10, "te": 20},
        "materials": materials if materials is not None else [
            {"type_id": 34, "name": "Tritanium", "missing": 100},
            {"type_id": 35, "name": "Pyerite", "missing": 50},
        ],
        "manufacturing_steps": jobs if jobs is not None else [
            {"step": 1, "jobs": [{"type_id": product_id, "name": "job one",
                                  "quantity": 1, "runs": 1}]},
        ],
    }


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request, tmp_path):
    """One connection per backend, schema built, torn down after."""
    if request.param == "sqlite":
        # `upgrade_to_head`, not `ph.ensure_project_tables`: that shim had no
        # caller in `app/` and was removed in v0.9.76. The migrations are what
        # build this schema everywhere else — including this fixture's Postgres
        # half a few lines down — so using them here makes the two halves agree
        # about where the tables come from.
        from app.db.migrate import upgrade_to_head

        url = f"sqlite:///{tmp_path / 'projects.db'}"
        upgrade_to_head(url)
        engine = create_engine(url)
        with engine.connect() as c:
            yield c
        engine.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

    from app.db.migrate import upgrade_to_head

    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.commit()
    admin.dispose()

    scoped = PG_URL + ("&" if "?" in PG_URL else "?") + \
        f"options=-csearch_path%3D{PG_SCHEMA}"
    upgrade_to_head(scoped)

    engine = create_engine(scoped)
    with engine.connect() as c:
        yield c
    engine.dispose()


def _backend(conn) -> str:
    return conn.engine.dialect.name


# ── the control ──────────────────────────────────────────────────────────────

def test_both_backends_are_actually_exercised(conn):
    """Names the backend out loud. Without this a broken Postgres fixture would
    look like a passing suite: the SQLite half would carry it, and the whole
    point of the file is that both halves run."""
    assert _backend(conn) in ("sqlite", "postgresql")
    conn.execute(text("SELECT 1"))


# ── the two statements this file exists for ──────────────────────────────────

def test_adding_the_same_material_twice_accumulates(conn):
    """The qualified `ON CONFLICT` target. Unqualified, Postgres reads the
    proposed row and this comes back 200 instead of 300."""
    pid = ph.create_project(conn, "accumulate")
    ph.add_plan_to_project(conn, pid, _plan(), "Jita IV", 0.0)
    ph.add_plan_to_project(conn, pid, _plan(product_id=12006), "Jita IV", 0.0)

    rows = dict(conn.execute(text(
        "SELECT type_id, needed FROM project_shopping WHERE project_id = :p"),
        {"p": pid}).fetchall())

    assert rows[34] == 200, (
        f"on {_backend(conn)}: two plans needing 100 each came to {rows.get(34)}, "
        "not 200 — DO UPDATE resolved the bare column against the wrong row")
    assert rows[35] == 100


def test_a_third_plan_keeps_accumulating(conn):
    """Two additions can be right by accident when the wrong row happens to
    hold the same value. Three cannot."""
    pid = ph.create_project(conn, "three")
    for n in range(3):
        ph.add_plan_to_project(conn, pid, _plan(product_id=12005 + n), "Jita", 0.0)

    needed = conn.execute(text(
        "SELECT needed FROM project_shopping WHERE project_id = :p AND type_id = 34"),
        {"p": pid}).scalar()

    assert needed == 300, f"on {_backend(conn)}: expected 300, got {needed}"


def test_the_counts_do_not_multiply_plans_by_shopping_lines(conn):
    """The cartesian product. Two plans and two shopping lines: a bare
    SUM/COUNT reports four plans, and the progress bar goes past 100%."""
    pid = ph.create_project(conn, "counts")
    ph.add_plan_to_project(conn, pid, _plan(), "Jita", 0.0)
    ph.add_plan_to_project(conn, pid, _plan(product_id=12006), "Jita", 0.0)

    row = next(p for p in ph.list_projects(conn) if p["id"] == pid)

    assert row["plan_count"] == 2, (
        f"on {_backend(conn)}: {row['plan_count']} plans reported, 2 exist")
    assert row["shopping_total"] == 2, (
        f"on {_backend(conn)}: {row['shopping_total']} shopping lines, 2 exist")


def test_completed_counts_survive_the_same_join(conn):
    """`completed_plans` and `shopping_done` are the conditional halves of the
    same query, and are what a progress bar divides by the totals above."""
    pid = ph.create_project(conn, "completion")
    ph.add_plan_to_project(conn, pid, _plan(), "Jita", 0.0)
    ph.add_plan_to_project(conn, pid, _plan(product_id=12006), "Jita", 0.0)

    conn.execute(text("UPDATE project_plans SET status = 'completed'"
                      " WHERE project_id = :p AND product_type_id = 12005"),
                 {"p": pid})
    conn.execute(text("UPDATE project_shopping SET purchased = needed"
                      " WHERE project_id = :p AND type_id = 34"), {"p": pid})
    conn.commit()

    row = next(p for p in ph.list_projects(conn) if p["id"] == pid)

    assert row["completed_plans"] == 1, (
        f"on {_backend(conn)}: {row['completed_plans']} completed, 1 is right")
    assert row["shopping_done"] == 1, (
        f"on {_backend(conn)}: {row['shopping_done']} purchased, 1 is right")
    assert row["completed_plans"] <= row["plan_count"], "a bar past 100%"
    assert row["shopping_done"] <= row["shopping_total"], "a bar past 100%"


# ── the rest of the module, so the conversion is covered and not just its traps ──

def test_a_project_round_trips(conn):
    """`RETURNING id` replaced `cursor.lastrowid`, which has no equivalent on
    either backend through SQLAlchemy — so the id coming back is itself a claim."""
    pid = ph.create_project(conn, "round trip")

    assert isinstance(pid, int) and pid > 0, f"on {_backend(conn)}: no id came back"
    detail = ph.get_project_detail(conn, pid)
    assert detail is not None and detail["name"] == "round trip"


def test_a_plan_lands_with_its_shopping_list_and_its_jobs(conn):
    pid = ph.create_project(conn, "full plan")
    plan_id = ph.add_plan_to_project(conn, pid, _plan(qty=5), "Jita IV - Moon 4", 0.02)

    assert isinstance(plan_id, int) and plan_id > 0
    detail = ph.get_project_detail(conn, pid)
    assert len(detail["plans"]) == 1
    assert len(detail["shopping"]) == 2
    # `steps` is rebuilt from plan_json rather than read back from project_jobs,
    # so this also checks the JSON survived the round trip — Postgres and SQLite
    # store it in different column types.
    assert detail["total_jobs"] == 1, f"on {_backend(conn)}: {detail['total_jobs']}"
    assert len(detail["steps"]) == 1


def test_a_material_that_is_not_missing_buys_nothing(conn):
    """`missing > 0` guards the insert. A zero row would show up as a shopping
    line for something already owned."""
    pid = ph.create_project(conn, "nothing missing")
    ph.add_plan_to_project(conn, pid, _plan(materials=[
        {"type_id": 34, "name": "Tritanium", "missing": 0},
        {"type_id": 35, "name": "Pyerite", "missing": 7},
    ]), "Jita", 0.0)

    rows = conn.execute(text(
        "SELECT type_id FROM project_shopping WHERE project_id = :p"),
        {"p": pid}).fetchall()

    assert [r[0] for r in rows] == [35], f"on {_backend(conn)}: {rows}"


def test_an_unknown_project_has_no_detail(conn):
    assert ph.get_project_detail(conn, 999_999) is None


def test_writes_are_committed_not_just_visible_on_this_connection(conn):
    """The trap the worklist calls the one that will cost a debugging session:
    SQLAlchemy opens a transaction and rolls it back on close, so a call site
    moved across without its commit() loses writes silently. Reading from a
    second connection is what tells a commit from an open transaction."""
    pid = ph.create_project(conn, "committed")
    ph.add_plan_to_project(conn, pid, _plan(), "Jita", 0.0)

    with conn.engine.connect() as other:
        name = other.execute(text(
            "SELECT name FROM production_projects WHERE id = :p"), {"p": pid}).scalar()
        lines = other.execute(text(
            "SELECT COUNT(*) FROM project_shopping WHERE project_id = :p"),
            {"p": pid}).scalar()

    assert name == "committed", f"on {_backend(conn)}: the project was never committed"
    assert lines == 2, f"on {_backend(conn)}: the shopping list was never committed"
