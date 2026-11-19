"""job_runs: one row per job execution (start, finish, ok, detail text/JSON)."""
from __future__ import annotations

from datetime import datetime

from .. import db
from ._sql import now_utc, query, utc_naive


def start_run(job: str) -> int:
    """Record a job start. Returns run id."""
    with db.write() as cur:
        return cur.execute("INSERT INTO job_runs (job, started_at) VALUES (?, ?) RETURNING id",
                           [job, now_utc()]).fetchone()[0]


def update_run(run_id: int, detail: str) -> None:
    """Set the detail of a running job (progress)."""
    with db.write() as cur:
        cur.execute("UPDATE job_runs SET detail = ? WHERE id = ?", [detail, run_id])


def finish_run(run_id: int, ok: bool, detail: str | None = None) -> None:
    """Record a job's end; detail None keeps any progress detail."""
    with db.write() as cur:
        cur.execute("UPDATE job_runs SET finished_at = ?, ok = ?, detail = COALESCE(?, detail) WHERE id = ?",
                    [now_utc(), ok, detail, run_id])


def recent_runs(limit: int = 50, job: str | None = None) -> list[dict]:
    """-> [{id, job, started_at, finished_at, ok, detail}] newest first."""
    return query("SELECT * FROM job_runs WHERE (?::VARCHAR IS NULL OR job = ?) ORDER BY id DESC LIMIT ?",
                 [job, job, limit])


def latest_run(job: str, ok: bool | None = None) -> dict | None:
    """Most recent run of a job (optionally only successful/failed ones) or None."""
    rows = query("""SELECT * FROM job_runs WHERE job = ? AND (?::BOOLEAN IS NULL OR ok = ?)
                    ORDER BY id DESC LIMIT 1""", [job, ok, ok])
    return rows[0] if rows else None


def latest_per_job() -> dict[str, dict]:
    """-> {job: latest run row}."""
    return {r["job"]: r for r in query(
        "SELECT * FROM job_runs QUALIFY row_number() OVER (PARTITION BY job ORDER BY id DESC) = 1")}


def prune_runs(before: datetime) -> int:
    """Delete finished runs started before `before`. Returns rows deleted. Keeps the latest run per job."""
    with db.write() as cur:
        cur.execute("""DELETE FROM job_runs WHERE started_at < ? AND finished_at IS NOT NULL
                       AND id NOT IN (SELECT max(id) FROM job_runs GROUP BY job)""", [utc_naive(before)])
        return cur.fetchone()[0]
