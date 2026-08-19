"""Production Projects, end to end through the HTTP API.

Written because the conversion to the portable query layer (Step 4) started
here and found the module had no coverage at all beyond `/projects` returning
200. Eight write endpoints, none of them exercised.

That matters more for this change than for most. SQLAlchemy opens a transaction
on first use and rolls it back when the connection closes, while `sqlite3` in
its default isolation mode commits some statements for you — so a converted
write that lost its `commit()` still returns `{"ok": true}` and still passes
any test that only checks the response. Every test here reads the row back
through a *new* request, which is a new connection, which is the only thing
that distinguishes committed from merely written.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def project(client):
    """A project that cleans itself up, whatever the test does to it."""
    r = client.post("/api/projects/new", json={"name": "Conversion Test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    pid = body["project_id"]
    yield pid
    client.delete(f"/api/projects/{pid}")


def _plan_data():
    """The shape /plan posts: a product, its shortfalls, and its job steps."""
    return {
        "product_type_id": 641,
        "product_name": "Megathron",
        "quantity": 2,
        "blueprint": {"me": 7, "te": 14},
        "materials": [
            {"type_id": 34, "name": "Tritanium", "missing": 1000},
            {"type_id": 35, "name": "Pyerite", "missing": 250},
            {"type_id": 36, "name": "Mexallon", "missing": 0},   # nothing to buy
        ],
        "manufacturing_steps": [
            {"step": 1, "jobs": [
                {"type_id": 641, "name": "Megathron", "quantity": 2, "runs": 2,
                 "activity": "manufacturing",
                 "inputs": [{"type_id": 34, "name": "Tritanium", "quantity": 1000,
                             "is_leaf": True, "activity": ""}]},
            ]},
        ],
    }


def _detail(client, pid):
    """The project as the API reports it — a fresh request, so a fresh
    connection. A write that was never committed does not survive this."""
    listing = client.get("/api/projects/list").json()["projects"]
    return next((p for p in listing if p["id"] == pid), None)


def test_a_new_project_is_still_there_on_the_next_request(client, project):
    assert _detail(client, project) is not None, (
        "the project did not survive the request that created it — "
        "the INSERT was never committed"
    )


def test_the_name_comes_back_as_it_went_in(client, project):
    row = _detail(client, project)
    assert row["name"] == "Conversion Test"


def test_a_plan_lands_with_its_shopping_list_and_its_jobs(client, project):
    r = client.post(f"/api/projects/{project}/add-plan",
                    json={"plan_data": _plan_data(), "station_name": "Jita IV-4",
                          "facility_tax": 1.5})
    assert r.status_code == 200 and r.json()["ok"], r.text

    page = client.get(f"/projects/{project}").text
    assert "Megathron" in page
    assert "Tritanium" in page and "Pyerite" in page
    # missing == 0 buys nothing, so it must not appear as a shopping line
    assert "Mexallon" not in page

    row = _detail(client, project)
    assert row["plan_count"] == 1
    assert row["shopping_total"] == 2, "a zero-shortfall material was still listed"


def test_adding_the_same_plan_twice_adds_up_the_shortfall(client, project):
    """The shopping upsert is `ON CONFLICT ... DO UPDATE SET needed = needed +
    excluded.needed`, and an unqualified `needed` there means the proposed row
    on Postgres and the stored row on SQLite. Qualifying it is the fix; this is
    what would catch it going wrong."""
    for _ in range(2):
        client.post(f"/api/projects/{project}/add-plan",
                    json={"plan_data": _plan_data(), "station_name": "", "facility_tax": 0})

    from app.db.conn import connect
    from sqlalchemy import text

    # Asserted against the stored value, not the rendered page: the first
    # version of this test looked for "2000" in the HTML and passed even with
    # the increment removed, because that string appears elsewhere on it.
    with connect() as conn:
        needed = conn.execute(
            text("SELECT needed FROM project_shopping"
                 " WHERE project_id = :p AND type_id = :t"),
            {"p": project, "t": 34}).scalar()
    assert needed == 2000, (
        f"two plans needing 1000 Tritanium each came to {needed}, not 2000"
    )


def test_add_plan_refuses_a_project_that_does_not_exist(client):
    r = client.post("/api/projects/999999/add-plan",
                    json={"plan_data": _plan_data()})
    assert r.json() == {"ok": False, "error": "Project not found"}


def test_add_plan_refuses_an_empty_body(client, project):
    r = client.post(f"/api/projects/{project}/add-plan", json={})
    assert r.json()["ok"] is False


def test_a_completed_job_stays_completed(client, project):
    client.post(f"/api/projects/{project}/add-plan",
                json={"plan_data": _plan_data(), "station_name": "", "facility_tax": 0})
    page = client.get(f"/projects/{project}").text
    assert "Megathron" in page

    from app.db.conn import connect
    from sqlalchemy import text

    with connect() as conn:
        job_ids = [r[0] for r in conn.execute(
            text("SELECT id FROM project_jobs WHERE project_id = :p"),
            {"p": project}).fetchall()]
    assert job_ids, "adding a plan wrote no jobs"

    r = client.post("/api/project-jobs/toggle",
                    json={"job_ids": job_ids, "status": "completed"})
    assert r.json() == {"ok": True, "status": "completed"}

    with connect() as conn:
        statuses = {r[0] for r in conn.execute(
            text("SELECT status FROM project_jobs WHERE project_id = :p"),
            {"p": project}).fetchall()}
    assert statuses == {"completed"}, f"the toggle did not persist: {statuses}"


def test_the_job_toggle_takes_a_list_of_any_length(client, project):
    """The IN clause used to be a hand-built string of `?`s. `expanding` binds
    replace it, and an empty or single-element list is where that shows."""
    client.post(f"/api/projects/{project}/add-plan",
                json={"plan_data": _plan_data(), "station_name": "", "facility_tax": 0})

    from app.db.conn import connect
    from sqlalchemy import text

    with connect() as conn:
        job_ids = [r[0] for r in conn.execute(
            text("SELECT id FROM project_jobs WHERE project_id = :p"),
            {"p": project}).fetchall()]

    assert client.post("/api/project-jobs/toggle",
                       json={"job_ids": job_ids[:1], "status": "completed"}).json()["ok"]
    assert client.post("/api/project-jobs/toggle",
                       json={"job_ids": [], "status": "completed"}).json()["ok"] is False


def test_the_toggle_refuses_a_status_it_does_not_know(client, project):
    r = client.post("/api/project-jobs/toggle",
                    json={"job_ids": [1], "status": "abandoned"})
    assert r.json()["ok"] is False


def test_a_purchase_is_remembered(client, project):
    client.post(f"/api/projects/{project}/add-plan",
                json={"plan_data": _plan_data(), "station_name": "", "facility_tax": 0})

    r = client.post("/api/project-shopping/update",
                    json={"project_id": project, "type_id": 34, "purchased": 400})
    assert r.json() == {"ok": True}

    from app.db.conn import connect
    from sqlalchemy import text

    with connect() as conn:
        got = conn.execute(
            text("SELECT purchased FROM project_shopping"
                 " WHERE project_id = :p AND type_id = :t"),
            {"p": project, "t": 34}).scalar()
    assert got == 400, "the purchase was not committed"


def test_mark_all_fills_every_line(client, project):
    client.post(f"/api/projects/{project}/add-plan",
                json={"plan_data": _plan_data(), "station_name": "", "facility_tax": 0})
    assert client.post(f"/api/projects/{project}/shopping/mark-all").json() == {"ok": True}

    row = _detail(client, project)
    assert row["shopping_done"] == row["shopping_total"] == 2


def test_a_completed_plan_stays_completed(client, project):
    client.post(f"/api/projects/{project}/add-plan",
                json={"plan_data": _plan_data(), "station_name": "", "facility_tax": 0})

    from app.db.conn import connect
    from sqlalchemy import text

    with connect() as conn:
        plan_id = conn.execute(
            text("SELECT id FROM project_plans WHERE project_id = :p"),
            {"p": project}).scalar()

    assert client.post(f"/api/project-plans/{plan_id}/toggle",
                       json={"status": "completed"}).json()["ok"]
    assert _detail(client, project)["completed_plans"] == 1


def test_deleting_a_project_takes_its_children_with_it(client):
    pid = client.post("/api/projects/new", json={"name": "Doomed"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/add-plan",
                json={"plan_data": _plan_data(), "station_name": "", "facility_tax": 0})

    assert client.delete(f"/api/projects/{pid}").json() == {"ok": True}
    assert _detail(client, pid) is None

    from app.db.conn import connect
    from sqlalchemy import text

    with connect() as conn:
        for table in ("project_jobs", "project_shopping", "project_plans"):
            left = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE project_id = :p"),
                {"p": pid}).scalar()
            assert left == 0, f"{table} kept {left} orphaned rows"


def test_a_project_needs_a_name(client):
    r = client.post("/api/projects/new", json={"name": "   "})
    assert r.json()["ok"] is False


def test_a_missing_project_page_is_a_404(client):
    assert client.get("/projects/999999").status_code == 404


# ── the counts on the project list ───────────────────────────────────────────

def test_the_counts_do_not_multiply_plans_by_shopping_lines(client):
    """Found while converting this module, and live before it.

    `list_projects` LEFT JOINs project_plans and project_shopping off the same
    row, which is a cartesian product: 2 plans and 3 shopping lines give 6
    rows. `COUNT(DISTINCT ...)` kept the totals right, but the two
    `SUM(CASE WHEN ... THEN 1 END)` columns counted each plan once per shopping
    line — so a project with any plans *and* any shopping showed more complete
    than it had, and the progress bar went past 100%.

    One plan hides it, which is why nothing caught it: 1 x N shopping lines
    still gives N.
    """
    pid = client.post("/api/projects/new", json={"name": "Fan Out"}).json()["project_id"]
    try:
        # Two plans, so the join fans out; the shopping list has two lines.
        for _ in range(2):
            client.post(f"/api/projects/{pid}/add-plan",
                        json={"plan_data": _plan_data(), "station_name": "",
                              "facility_tax": 0})
        client.post(f"/api/projects/{pid}/shopping/mark-all")

        from app.db.conn import connect
        from sqlalchemy import text

        with connect() as conn:
            for plan_id, in conn.execute(
                    text("SELECT id FROM project_plans WHERE project_id = :p"),
                    {"p": pid}).fetchall():
                client.post(f"/api/project-plans/{plan_id}/toggle",
                            json={"status": "completed"})

        row = _detail(client, pid)
        assert row["plan_count"] == 2
        assert row["shopping_total"] == 2
        assert row["completed_plans"] == 2, (
            f"2 plans reported as {row['completed_plans']} complete — the join "
            "is counting each plan once per shopping line"
        )
        assert row["shopping_done"] == 2, (
            f"2 shopping lines reported as {row['shopping_done']} bought"
        )
        assert row["completed_plans"] <= row["plan_count"]
        assert row["shopping_done"] <= row["shopping_total"]
    finally:
        client.delete(f"/api/projects/{pid}")
