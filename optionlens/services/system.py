"""Health, status page data and symbol search."""
from __future__ import annotations

from datetime import timedelta

from ..analytics.calendar import is_market_open, now_ny
from ..jobs import scheduler
from ..jobs.calendars import earnings_backfill_progress
from ..repo import jobs as jobs_repo
from ..repo import options as options_repo
from ..repo import status as status_repo
from ..repo import symbols as symbols_repo

SEARCH_LIMIT = 20
JOB_FIELDS = ("job", "started_at", "finished_at", "ok", "detail")


def _job(r: dict) -> dict:
    return {k: r[k] for k in JOB_FIELDS}


def status() -> dict:
    now = now_ny()
    counts = symbols_repo.counts()
    sweep = jobs_repo.latest_run("sweep", ok=True)
    latest = sorted(jobs_repo.latest_per_job().values(), key=lambda r: r["job"])
    seen = {r["id"] for r in latest}
    recent = [r for r in jobs_repo.recent_runs(50) if r["id"] not in seen]
    return {
        "now": now, "market_open": is_market_open(now), "next_sweep_at": scheduler.next_run_at("sweep", now),
        "universe_size": counts["universe_size"], "optionable_count": counts["optionable_count"],
        "last_sweep_at": sweep["finished_at"] if sweep else None,
        "last_eod_date": options_repo.prior_eod_date(now.date() + timedelta(days=1)),
        "iv_history_days": status_repo.distinct_dates("metrics_daily"),
        "option_eod_days": status_repo.distinct_dates("option_eod"),
        "earnings_backfill_progress": earnings_backfill_progress(),
        "row_counts": status_repo.row_counts(),
        "jobs": [_job(r) for r in latest + recent],
        "db_size_bytes": status_repo.db_size_bytes(),
    }


def search(q: str) -> list[dict]:
    return symbols_repo.search_symbols(q, SEARCH_LIMIT)
