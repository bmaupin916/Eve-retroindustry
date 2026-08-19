"""Projects: the saved-plan collections, their pages and their APIs.

Moved out of `main.py` unchanged (W6), then converted to the portable query
layer (Step 4). Its connections come from `app.db.conn.connect()` rather than
`deps.get_conn()`, and every statement uses named binds — so the same SQL runs
on SQLite and on Postgres.

The rest of the app has not moved yet, and does not have to: both styles work
on one database at the same time. `app/web/projects_helper.py` carries the
notes on what changes at a call site.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import bindparam, text

from app.db.conn import connect
from app.web.deps import _tr
from app.web.projects_helper import (
    add_plan_to_project,
    create_project,
    get_project_detail,
    list_projects,
)

router = APIRouter()


@router.get("/projects", response_class=HTMLResponse)
async def projects_list(request: Request):
    with connect() as conn:
        projects = list_projects(conn)
    return _tr("projects.html", request, {"projects": projects})


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(request: Request, project_id: int):
    with connect() as conn:
        detail = get_project_detail(conn, project_id)
    if not detail:
        return HTMLResponse("Project not found", status_code=404)
    return _tr("project_detail.html", request, {"project": detail})


@router.get("/api/projects/list")
async def api_projects_list():
    with connect() as conn:
        return {"projects": list_projects(conn)}


@router.post("/api/projects/new")
async def api_project_new(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "The name must not be empty"}
    with connect() as conn:
        pid = create_project(conn, name)
    return {"ok": True, "project_id": pid, "name": name}


@router.post("/api/projects/{project_id}/add-plan")
async def api_project_add_plan(project_id: int, request: Request):
    body = await request.json()
    plan_data = body.get("plan_data")
    if not plan_data:
        return {"ok": False, "error": "Missing plan data"}
    with connect() as conn:
        exists = conn.execute(
            text("SELECT id FROM production_projects WHERE id = :id"),
            {"id": project_id},
        ).fetchone()
        if not exists:
            return {"ok": False, "error": "Project not found"}
        plan_id = add_plan_to_project(
            conn, project_id, plan_data,
            body.get("station_name", ""),
            float(body.get("facility_tax", 0)),
        )
    return {"ok": True, "plan_id": plan_id}


@router.post("/api/project-jobs/toggle")
async def api_project_job_toggle(request: Request):
    """Toggle status of one or more job IDs (merged jobs share type_id+step)."""
    body = await request.json()
    job_ids = body.get("job_ids", [])
    target = body.get("status")  # "completed" or "pending"
    if not job_ids or target not in ("completed", "pending"):
        return {"ok": False, "error": "bad request"}
    with connect() as conn:
        # `expanding` renders the IN list for whichever driver is underneath.
        # The old version built the placeholder string by hand — one `?` per
        # id — which is exactly the construction that does not survive a move
        # to a driver with a different paramstyle.
        stmt = text("UPDATE project_jobs SET status = :status WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        conn.execute(stmt, {"status": target, "ids": [int(j) for j in job_ids]})
        conn.commit()
    return {"ok": True, "status": target}


@router.post("/api/project-shopping/update")
async def api_project_shopping_update(request: Request):
    body = await request.json()
    project_id = int(body.get("project_id", 0))
    type_id = int(body.get("type_id", 0))
    purchased = int(body.get("purchased", 0))
    if not project_id or not type_id:
        return {"ok": False}
    with connect() as conn:
        conn.execute(
            text("UPDATE project_shopping SET purchased = :purchased"
                 " WHERE project_id = :project_id AND type_id = :type_id"),
            {"purchased": purchased, "project_id": project_id, "type_id": type_id},
        )
        conn.commit()
    return {"ok": True}


@router.post("/api/projects/{project_id}/shopping/mark-all")
async def api_project_shopping_mark_all(project_id: int):
    with connect() as conn:
        conn.execute(
            text("UPDATE project_shopping SET purchased = needed"
                 " WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        conn.commit()
    return {"ok": True}


@router.post("/api/project-plans/{plan_id}/toggle")
async def api_project_plan_toggle(plan_id: int, request: Request):
    body = await request.json()
    status = body.get("status", "completed")
    with connect() as conn:
        conn.execute(
            text("UPDATE project_plans SET status = :status WHERE id = :id"),
            {"status": status, "id": plan_id},
        )
        conn.commit()
    return {"ok": True, "status": status}


# Table -> the column that names the project. Written out rather than derived
# so an f-string never carries anything but a value from this dict; the old
# version interpolated both the table and the column into the statement.
_PROJECT_TABLES = {
    "project_jobs": "project_id",
    "project_shopping": "project_id",
    "project_plans": "project_id",
    "production_projects": "id",
}


@router.delete("/api/projects/{project_id}")
async def api_project_delete(project_id: int):
    with connect() as conn:
        for table, column in _PROJECT_TABLES.items():
            conn.execute(text(f"DELETE FROM {table} WHERE {column} = :project_id"),
                         {"project_id": project_id})
        conn.commit()
    return {"ok": True}
