"""Production Projects: saved plans, their shopping lists and their job boards.

**The first module on the portable query layer** (Step 4, `app/db/conn.py`).
Every statement here uses named binds and takes a SQLAlchemy `Connection`, so
the same SQL runs on SQLite and on Postgres. The rest of the app still uses
`deps.get_conn()` and positional `?`, and the two coexist on one database —
which is what makes this conversion module-by-module rather than one tree-wide
commit. See `test_both_connection_styles_work_on_one_database`.

Two things change at every call site and both are easy to miss:

* **Writes need an explicit `commit()`.** SQLAlchemy opens a transaction on
  first use and rolls it back when the connection closes. `sqlite3` in its
  default isolation mode commits some statements for you, so a converted write
  without a commit is lost *silently*.
* **`cursor.lastrowid` is gone.** `RETURNING id` replaces it, which needs
  SQLite >= 3.35 — asserted by `test_returning_replaces_lastrowid`.
"""
import json
import time
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.engine import Connection



def list_projects(conn: Connection) -> list[dict]:
    # Two LEFT JOINs off the same row is a cartesian product: a project with 2
    # plans and 3 shopping lines produces 6 rows, so a bare
    # SUM(CASE WHEN ... THEN 1 END) counts each plan once per shopping line.
    # That is what this query used to do — it reported "6 of 2 plans complete"
    # and a progress bar past 100%. COUNT(DISTINCT CASE ...) counts the thing
    # itself rather than the rows it appears in.
    rows = conn.execute(text("""
        SELECT p.id, p.name, p.created_at, p.updated_at,
               COUNT(DISTINCT pl.id) AS plan_count,
               COUNT(DISTINCT CASE WHEN pl.status='completed' THEN pl.id END)
                   AS completed_plans,
               COUNT(DISTINCT ps.type_id) AS shopping_total,
               COUNT(DISTINCT CASE WHEN ps.purchased >= ps.needed AND ps.needed > 0
                                   THEN ps.type_id END) AS shopping_done
        FROM production_projects p
        LEFT JOIN project_plans pl ON pl.project_id = p.id
        LEFT JOIN project_shopping ps ON ps.project_id = p.id
        GROUP BY p.id, p.name, p.created_at, p.updated_at
        ORDER BY p.updated_at DESC
    """)).fetchall()
    return [
        {
            "id": r[0], "name": r[1], "created_at": r[2], "updated_at": r[3],
            "plan_count": r[4] or 0, "completed_plans": r[5] or 0,
            "shopping_total": r[6] or 0, "shopping_done": r[7] or 0,
        }
        for r in rows
    ]


def create_project(conn: Connection, name: str) -> int:
    now = time.time()
    new_id = conn.execute(
        text("INSERT INTO production_projects (name, created_at, updated_at)"
             " VALUES (:name, :now, :now) RETURNING id"),
        {"name": name, "now": now},
    ).scalar()
    conn.commit()
    return new_id


def add_plan_to_project(
    conn: Connection,
    project_id: int,
    plan_data: dict,
    station_name: str,
    facility_tax: float,
) -> int:
    now = time.time()
    bp = plan_data.get("blueprint") or {}
    plan_id = conn.execute(
        text("""
        INSERT INTO project_plans
        (project_id, product_type_id, product_name, quantity, me, te,
         station_name, facility_tax, plan_json, status, created_at)
        VALUES (:project_id, :product_type_id, :product_name, :quantity, :me, :te,
                :station_name, :facility_tax, :plan_json, :status, :created_at)
        RETURNING id
        """),
        {
            "project_id": project_id,
            "product_type_id": plan_data["product_type_id"],
            "product_name": plan_data["product_name"],
            "quantity": plan_data["quantity"],
            "me": bp.get("me", 0),
            "te": bp.get("te", 0),
            "station_name": station_name,
            "facility_tax": facility_tax,
            "plan_json": json.dumps(plan_data, default=str),
            "status": "pending",
            "created_at": now,
        },
    ).scalar()

    for mat in plan_data.get("materials", []):
        missing = mat.get("missing") or 0
        if missing > 0:
            # `needed` is qualified rather than bare. Inside DO UPDATE the name
            # is visible on both the target table and the `excluded` pseudo-row,
            # and Postgres refuses to guess: `needed = needed + excluded.needed`
            # raises `AmbiguousColumn: column reference "needed" is ambiguous`.
            # SQLite resolves it to the stored row and runs happily, so this is
            # a statement that works until the day it meets Postgres — verified
            # both ways in tests/test_projects_on_postgres.py.
            conn.execute(
                text("""
                INSERT INTO project_shopping (project_id, type_id, name, needed, purchased)
                VALUES (:project_id, :type_id, :name, :needed, 0)
                ON CONFLICT (project_id, type_id) DO UPDATE
                    SET needed = project_shopping.needed + excluded.needed,
                        name = excluded.name
                """),
                {"project_id": project_id, "type_id": mat["type_id"],
                 "name": mat["name"], "needed": missing},
            )

    for step_data in plan_data.get("manufacturing_steps", []):
        for job in step_data.get("jobs", []):
            conn.execute(
                text("""
                INSERT INTO project_jobs
                (plan_id, project_id, type_id, name, quantity, runs, step, activity, status)
                VALUES (:plan_id, :project_id, :type_id, :name, :quantity, :runs,
                        :step, :activity, :status)
                """),
                {
                    "plan_id": plan_id,
                    "project_id": project_id,
                    "type_id": job["type_id"],
                    "name": job["name"],
                    "quantity": job.get("quantity", 1),
                    "runs": job.get("runs", 1),
                    "step": step_data["step"],
                    "activity": job.get("activity", "manufacturing"),
                    "status": "pending",
                },
            )

    conn.execute(
        text("UPDATE production_projects SET updated_at = :now WHERE id = :id"),
        {"now": now, "id": project_id},
    )
    conn.commit()
    return plan_id


def get_project_detail(conn: Connection, project_id: int) -> dict | None:
    proj = conn.execute(
        text("SELECT id, name, created_at, updated_at FROM production_projects"
             " WHERE id = :id"),
        {"id": project_id},
    ).fetchone()
    if not proj:
        return None

    plans = [
        {
            "id": r[0], "product_type_id": r[1], "product_name": r[2],
            "quantity": r[3], "me": r[4], "te": r[5],
            "station_name": r[6], "facility_tax": r[7], "status": r[8], "created_at": r[9],
        }
        for r in conn.execute(
            text("""
            SELECT id, product_type_id, product_name, quantity, me, te,
                   station_name, facility_tax, status, created_at
            FROM project_plans WHERE project_id = :project_id ORDER BY created_at
            """),
            {"project_id": project_id},
        ).fetchall()
    ]

    shopping = [
        {"type_id": r[0], "name": r[1], "needed": r[2], "purchased": r[3]}
        for r in conn.execute(
            text("SELECT type_id, name, needed, purchased FROM project_shopping"
                 " WHERE project_id = :project_id ORDER BY name"),
            {"project_id": project_id},
        ).fetchall()
    ]

    # Load each job's inputs from the stored plan_json (aggregate across plans)
    # (step, type_id) -> {input_type_id: {name, quantity, is_leaf, activity}}
    plan_input_map: dict = {}
    for plan_id_row, plan_json_str in conn.execute(
        text("SELECT id, plan_json FROM project_plans WHERE project_id = :project_id"),
        {"project_id": project_id},
    ).fetchall():
        try:
            pd = json.loads(plan_json_str)
        except Exception:
            continue
        for step_data in pd.get("manufacturing_steps", []):
            sn = step_data["step"]
            for job in step_data.get("jobs", []):
                key = (sn, job["type_id"])
                if key not in plan_input_map:
                    plan_input_map[key] = {}
                for inp in job.get("inputs", []):
                    tid = inp["type_id"]
                    if tid not in plan_input_map[key]:
                        plan_input_map[key][tid] = {
                            "type_id": tid,
                            "name": inp["name"],
                            "quantity": inp.get("quantity", 0),
                            "is_leaf": inp.get("is_leaf", True),
                            "activity": inp.get("activity", ""),
                        }
                    else:
                        plan_input_map[key][tid]["quantity"] += inp.get("quantity", 0)

    # Jobs grouped by step, then merged by type_id within step
    jobs_raw = conn.execute(
        text("""
        SELECT id, plan_id, type_id, name, quantity, runs, step, activity, status
        FROM project_jobs WHERE project_id = :project_id ORDER BY step, name
        """),
        {"project_id": project_id},
    ).fetchall()

    # Merge jobs with same type_id+step
    merged: dict = {}  # (step, type_id) -> job dict
    for r in jobs_raw:
        jd = {
            "id": r[0], "plan_id": r[1], "type_id": r[2], "name": r[3],
            "quantity": r[4], "runs": r[5], "step": r[6], "activity": r[7], "status": r[8],
        }
        key = (jd["step"], jd["type_id"])
        if key not in merged:
            merged[key] = {**jd, "job_ids": [jd["id"]], "completed": jd["status"] == "completed"}
        else:
            merged[key]["quantity"] += jd["quantity"]
            merged[key]["runs"] += jd["runs"]
            merged[key]["job_ids"].append(jd["id"])
            if jd["status"] != "completed":
                merged[key]["completed"] = False

    # Add inputs to each merged job
    for key, job in merged.items():
        inputs = plan_input_map.get(key, {})
        job["inputs"] = sorted(inputs.values(), key=lambda x: x["name"])

    steps_map: dict = defaultdict(list)
    for key, job in merged.items():
        steps_map[key[0]].append(job)

    steps = []
    for step_num in sorted(steps_map.keys()):
        step_jobs = sorted(steps_map[step_num], key=lambda j: j["name"])
        steps.append({
            "step": step_num,
            "jobs": step_jobs,
            "all_done": all(j["completed"] for j in step_jobs),
        })

    total_jobs = sum(len(s["jobs"]) for s in steps)
    done_jobs = sum(1 for s in steps for j in s["jobs"] if j["completed"])

    return {
        "id": proj[0], "name": proj[1], "created_at": proj[2], "updated_at": proj[3],
        "plans": plans, "shopping": shopping, "steps": steps,
        "total_jobs": total_jobs, "done_jobs": done_jobs,
        "shopping_done": sum(
            1 for s in shopping if s["purchased"] >= s["needed"] and s["needed"] > 0
        ),
    }
