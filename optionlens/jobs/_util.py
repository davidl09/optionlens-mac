"""Helpers shared by collectors: session date and closing-data boundary, bounded concurrency,
progress reporting."""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable, Iterable, TypeVar

from ..analytics.calendar import NY, OPEN, close_time, is_trading_day, now_ny, prev_trading_day
from ..repo import jobs as jobs_repo

log = logging.getLogger("optionlens.jobs")
T = TypeVar("T")

# CBOE data is ~15 min delayed; a fetch this long after the close (5 min margin) carries the closing
# snapshot. The one boundary for "final" session data: EOD schedule/archive, daily bars, freshness.
FINAL_DELAY_MIN = 20

# Set by scheduler.run_job so long collectors can publish progress into job_runs.detail.
current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)


def session_date(now: datetime | None = None) -> date:
    """Trading session the current market data belongs to: today once the session opened, else the previous one."""
    ts = (now or now_ny()).astimezone(NY)
    d = ts.date()
    return d if is_trading_day(d) and ts.time() >= OPEN else prev_trading_day(d)


def session_close(day: date) -> datetime:
    """Close of a trading session (aware NY; 13:00 on early-close days)."""
    return datetime.combine(day, close_time(day), NY)


def final_since(day: date) -> datetime:
    """Earliest fetch time whose (delayed) data is the session's closing snapshot: close + FINAL_DELAY_MIN."""
    return session_close(day) + timedelta(minutes=FINAL_DELAY_MIN)


async def report_progress(detail: str) -> None:
    """Store progress detail on the current job run (no-op outside run_job)."""
    rid = current_run_id.get()
    if rid is not None:
        await asyncio.to_thread(jobs_repo.update_run, rid, detail)


async def run_bounded(items: Iterable[T], fn: Callable[[T], Awaitable[object]], limit: int,
                      label: str) -> tuple[int, list[T]]:
    """Run fn over items with at most `limit` in flight; errors are logged, never raised.
    Returns (ok count, failed items)."""
    sem = asyncio.Semaphore(limit)
    failed: list[T] = []
    ok = 0

    async def one(item: T) -> None:
        nonlocal ok
        async with sem:
            try:
                await fn(item)
                ok += 1
            except Exception as e:  # noqa: BLE001 - one bad symbol must not stop a batch
                failed.append(item)
                log.warning("%s %s failed: %s", label, item, e)

    await asyncio.gather(*(one(i) for i in items))
    return ok, failed
