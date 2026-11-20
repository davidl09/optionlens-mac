"""Retention: option_eod (OPTION_EOD_RETENTION_DAYS), job_runs 90d, notifications 180d, http_cache 30d."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ..analytics.calendar import now_ny
from ..config import get_settings
from ..repo import cache as cache_repo
from ..repo import jobs as jobs_repo
from ..repo import options as options_repo
from ..repo import user as user_repo

JOB_RUNS_DAYS, NOTIFICATIONS_DAYS, CACHE_DAYS = 90, 180, 30


async def prune() -> str:
    """Apply retention windows. Returns deleted counts."""
    now = datetime.now(timezone.utc)
    eod_cutoff = now_ny().date() - timedelta(days=get_settings().option_eod_retention_days)

    def run() -> dict[str, int]:
        return {
            "option_eod": options_repo.prune_option_eod(eod_cutoff),
            "job_runs": jobs_repo.prune_runs(now - timedelta(days=JOB_RUNS_DAYS)),
            "notifications": user_repo.prune_notifications(now - timedelta(days=NOTIFICATIONS_DAYS)),
            "http_cache": cache_repo.prune_cache(now - timedelta(days=CACHE_DAYS)),
        }

    return ", ".join(f"{k} -{v}" for k, v in (await asyncio.to_thread(run)).items())
