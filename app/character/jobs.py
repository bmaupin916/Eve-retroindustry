"""
Industry jobs — a character's running manufacturing/reaction/research jobs.

ESI: GET /characters/{id}/industry/jobs/?include_completed=true
Scope: esi-industry.read_character_jobs.v1
"""
from __future__ import annotations

import json
import time

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

ESI_BASE = "https://esi.evetech.net/latest"

#: How long a stored job list is treated as current. Industry jobs move on the
#: scale of hours, and the background worker refreshes every fifteen minutes,
#: so an hour is generous — this is the fallback for when the worker is off,
#: not the normal path.
CACHE_TTL = 3600.0

ACTIVITY_LABELS: dict[int, str] = {
    1: "Manufacturing",
    3: "TE Research",
    4: "ME Research",
    5: "Copying",
    7: "Reverse Engineering",
    8: "Invention",
    9: "Reactions",
    11: "Reactions",
}


def activity_label(activity_id: int) -> str:
    return ACTIVITY_LABELS.get(activity_id, f"Activity {activity_id}")


def load_cached_jobs(conn: Connection, char_id: int) -> tuple[list[dict] | None, float]:
    """(jobs, cached_at) from the local cache, or (None, 0) if there is none.

    Returns None rather than [] for "never fetched", because a page that shows
    "no jobs" when it simply has not looked yet is a page that lies.
    """
    row = conn.execute(
        text("SELECT data_json, cached_at FROM char_jobs_cache"
             " WHERE character_id=:cid"),
        {"cid": char_id},
    ).fetchone()
    if not row:
        return None, 0.0
    try:
        return json.loads(row[0]), float(row[1] or 0.0)
    except (ValueError, TypeError):
        return None, 0.0


def save_cached_jobs(conn: Connection, char_id: int, jobs: list[dict]) -> None:
    # No commit here, deliberately: `fetch_industry_jobs` owns the
    # transaction boundary and commits after calling this.
    conn.execute(
        text("INSERT INTO char_jobs_cache (character_id, data_json, cached_at)"
             " VALUES (:cid, :data, :cached_at)"
             " ON CONFLICT (character_id) DO UPDATE SET"
             " data_json=excluded.data_json, cached_at=excluded.cached_at"),
        {"cid": char_id, "data": json.dumps(jobs), "cached_at": time.time()},
    )


async def fetch_industry_jobs(client: httpx.AsyncClient, char_id: int, token: str,
                              include_completed: bool = True,
                              conn: Connection | None = None,
                              force_refresh: bool = False) -> list[dict] | None:
    """Return a character's industry jobs, or None if the fetch failed (so the
    caller can tell a real "no jobs" apart from a transient ESI error — e.g.
    during a background sync). include_completed=true also returns ready/
    delivered from the recent period; active ones are filtered in the view.

    With a connection it caches, the same way assets and blueprints do. The
    /jobs page does not call this at all any more; the background worker does.
    """
    if conn is not None and not force_refresh:
        cached, cached_at = load_cached_jobs(conn, char_id)
        if cached is not None and (time.time() - cached_at) < CACHE_TTL:
            return cached
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/industry/jobs/",
            params={"include_completed": str(include_completed).lower()},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            jobs = r.json()
            if conn is not None:
                save_cached_jobs(conn, char_id, jobs)
                conn.commit()
            return jobs
    except Exception:
        pass
    return None
