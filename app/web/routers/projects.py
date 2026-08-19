"""Projects: the saved-plan collections, their pages and their APIs.

Moved out of `main.py` unchanged (W6). Nothing here is shared with another
router — the handlers reach the database directly or through
`app.web.projects_helper`.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.deps import _tr, get_conn
from app.web.projects_helper import (
    add_plan_to_project,
    create_project,
    get_project_detail,
    list_projects,
)

router = APIRouter()


@router.get("/projects", response_class=HTMLResponse)
async def projects_list(request: Request):
    conn = get_conn()
    projects = list_projects(conn)
    conn.close()
    return _tr("projects.html", request, {"projects": projects})


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(request: Request, project_id: int):
    conn = get_conn()
    detail = get_project_detail(conn, project_id)
    conn.close()
    if not detail:
        return HTMLResponse("Project not found", status_code=404)
    return _tr("project_detail.html", request, {"project": detail})


@router.get("/api/projects/list")
async def api_projects_list():
    conn = get_conn()
    projects = list_projects(conn)
    conn.close()
    return {"projects": projects}


@router.post("/api/projects/new")
async def api_project_new(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "The name must not be empty"}
    conn = get_conn()
    pid = create_project(conn, name)
    conn.close()
    return {"ok": True, "project_id": pid, "name": name}


@router.post("/api/projects/{project_id}/add-plan")
async def api_project_add_plan(project_id: int, request: Request):
    body = await request.json()
    plan_data = body.get("plan_data")
    if not plan_data:
        return {"ok": False, "error": "Missing plan data"}
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM production_projects WHERE id=?", (project_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "Project not found"}
    plan_id = add_plan_to_project(
        conn, project_id, plan_data,
        body.get("station_name", ""),
        float(body.get("facility_tax", 0)),
    )
    conn.close()
    return {"ok": True, "plan_id": plan_id}


@router.post("/api/project-jobs/toggle")
async def api_project_job_toggle(request: Request):
    """Toggle status of one or more job IDs (merged jobs share type_id+step)."""
    body = await request.json()
    job_ids = body.get("job_ids", [])
    target = body.get("status")  # "completed" or "pending"
    if not job_ids or target not in ("completed", "pending"):
        return {"ok": False, "error": "bad request"}
    conn = get_conn()
    ph = ",".join("?" * len(job_ids))
    conn.execute(
        f"UPDATE project_jobs SET status=? WHERE id IN ({ph})",
        [target] + list(job_ids),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "status": target}


@router.post("/api/project-shopping/update")
async def api_project_shopping_update(request: Request):
    body = await request.json()
    project_id = int(body.get("project_id", 0))
    type_id = int(body.get("type_id", 0))
    purchased = int(body.get("purchased", 0))
    if not project_id or not type_id:
        return {"ok": False}
    conn = get_conn()
    conn.execute(
        "UPDATE project_shopping SET purchased=? WHERE project_id=? AND type_id=?",
        (purchased, project_id, type_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/api/projects/{project_id}/shopping/mark-all")
async def api_project_shopping_mark_all(project_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE project_shopping SET purchased=needed WHERE project_id=?", (project_id,)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/api/project-plans/{plan_id}/toggle")
async def api_project_plan_toggle(plan_id: int, request: Request):
    body = await request.json()
    status = body.get("status", "completed")
    conn = get_conn()
    conn.execute("UPDATE project_plans SET status=? WHERE id=?", (status, plan_id))
    conn.commit()
    conn.close()
    return {"ok": True, "status": status}


@router.delete("/api/projects/{project_id}")
async def api_project_delete(project_id: int):
    conn = get_conn()
    for tbl in ("project_jobs", "project_shopping", "project_plans", "production_projects"):
        col = "id" if tbl == "production_projects" else "project_id"
        conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (project_id,))
    conn.commit()
    conn.close()
    return {"ok": True}
