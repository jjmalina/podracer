import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from podracer.db import (
    cancel_job,
    get_blocked_jobs,
    get_done_jobs,
    get_failed_jobs,
    get_job_counts,
    get_queued_jobs,
    get_running_jobs,
    get_worker_last_sync,
    retry_job,
)
from podracer.web.deps import get_db

router = APIRouter(prefix="/jobs")


def _enrich(conn, jobs):
    """Attach episode title and podcast name for display. Returns a list of
    dicts with the job fields plus episode_title and podcast_title."""
    if not jobs:
        return []
    ids = [j.episode_id for j in jobs]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT e.id, e.title AS episode_title, p.title AS podcast_title
            FROM episodes e JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.id IN ({placeholders})""",
        ids,
    ).fetchall()
    titles = {r["id"]: (r["episode_title"], r["podcast_title"]) for r in rows}
    result = []
    for j in jobs:
        ep_title, podcast_title = titles.get(j.episode_id, ("(unknown)", ""))
        result.append({
            "id": j.id,
            "episode_id": j.episode_id,
            "kind": j.kind,
            "status": j.status,
            "attempts": j.attempts,
            "max_attempts": j.max_attempts,
            "last_error": j.last_error,
            "depends_on_job_id": j.depends_on_job_id,
            "created_at": j.created_at,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
            "episode_title": ep_title,
            "podcast_title": podcast_title,
        })
    return result


@router.get("")
def jobs_list(request: Request, flash: str | None = None, db: sqlite3.Connection = Depends(get_db)):
    cfg = request.app.state.cfg
    return request.app.state.templates.TemplateResponse(request, "jobs/list.html", {
        "request": request,
        "counts": get_job_counts(db),
        "running": _enrich(db, get_running_jobs(db)),
        "queued": _enrich(db, get_queued_jobs(db, limit=20)),
        "failed": _enrich(db, get_failed_jobs(db, limit=10)),
        "blocked": _enrich(db, get_blocked_jobs(db, limit=10)),
        "done": _enrich(db, get_done_jobs(db, limit=10)),
        "last_sync": get_worker_last_sync(db),
        "sync_interval_minutes": cfg.sync_interval_minutes,
        "drain_interval_seconds": cfg.drain_interval_seconds,
        "flash": flash,
    })


@router.post("/{job_id}/retry")
def retry(request: Request, job_id: int, db: sqlite3.Connection = Depends(get_db)):
    ok = retry_job(db, job_id)
    flash = f"retried-{job_id}" if ok else f"not-retriable-{job_id}"
    return RedirectResponse(url=f"/jobs?flash={flash}", status_code=303)


@router.post("/{job_id}/cancel")
def cancel(request: Request, job_id: int, db: sqlite3.Connection = Depends(get_db)):
    ok = cancel_job(db, job_id)
    flash = f"cancelled-{job_id}" if ok else f"not-cancellable-{job_id}"
    return RedirectResponse(url=f"/jobs?flash={flash}", status_code=303)
