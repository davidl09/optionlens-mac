"""Asyncio job scheduler (America/New_York, NYSE trading-day aware). No external scheduler library.

Every job runs through `run_job`, which records the run in job_runs, logs failures and never lets a
job overlap itself. `start()`/`stop()` are called from the FastAPI lifespan; the CLI calls `run_job`.
On start, jobs whose most recent scheduled time passed without a successful run are caught up
(universe first); a failed run of a slot is retried every RETRY_BACKOFF, at most MAX_ATTEMPTS runs per slot.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable

from .. import net
from ..analytics.calendar import NY, close_time, is_trading_day, now_ny
from ..config import get_settings
from ..repo import jobs as jobs_repo
from ..sources import chain_source
from . import calendars, chains, maintenance, marketdata, prices, universe
from ._util import EOD_DEADLINE, FINAL_DELAY_MIN, current_run_id

log = logging.getLogger("optionlens.scheduler")

SEARCH_DAYS = 15
MAX_SLEEP_S = 60.0     # re-check the clock at least every minute (suspend/resume, clock jumps)
RETRY_BACKOFF = timedelta(minutes=15)   # wait after a failed run before retrying its slot
MAX_ATTEMPTS = 3                        # runs per scheduled slot (first + retries) before waiting for the next


# ----------------------------------------------------------------------------- schedules
@dataclass(frozen=True)
class Schedule:
    """`times_on(d)` -> NY wall-clock times a job fires on date d (empty when it does not)."""
    times_on: Callable[[date], list[time]]
    label: str

    def _candidates(self, d: date) -> list[datetime]:
        return [datetime.combine(d, t, NY) for t in sorted(self.times_on(d))]

    def next_after(self, ts: datetime) -> datetime | None:
        """First fire time strictly after ts (aware NY), or None within SEARCH_DAYS."""
        ts = ts.astimezone(NY)
        for i in range(SEARCH_DAYS):
            for c in self._candidates(ts.date() + timedelta(days=i)):
                if c > ts:
                    return c
        return None

    def prev_before(self, ts: datetime) -> datetime | None:
        """Last fire time at or before ts (aware NY), or None within SEARCH_DAYS."""
        ts = ts.astimezone(NY)
        for i in range(SEARCH_DAYS):
            for c in reversed(self._candidates(ts.date() - timedelta(days=i))):
                if c <= ts:
                    return c
        return None


def daily(at: time, weekdays: set[int] | None = None, trading_only: bool = False) -> Schedule:
    """Fixed time each day (optionally only given weekdays Mon=0, or only trading days)."""
    def times(d: date) -> list[time]:
        if weekdays is not None and d.weekday() not in weekdays:
            return []
        if trading_only and not is_trading_day(d):
            return []
        return [at]
    days = "trading days" if trading_only else ("days " + ",".join(map(str, sorted(weekdays))) if weekdays else "daily")
    return Schedule(times, f"{days} {at:%H:%M}")


def intraday(interval_min: int, first: time = time(9, 45)) -> Schedule:
    """Trading days every interval_min (>= 1) from `first` until the (possibly early) close, inclusive."""
    if interval_min < 1:
        raise ValueError(f"intraday interval must be >= 1 minute, got {interval_min}")

    def times(d: date) -> list[time]:
        if not is_trading_day(d):
            return []
        out, t, end = [], datetime.combine(d, first), datetime.combine(d, close_time(d))
        while t <= end:
            out.append(t.time())
            t += timedelta(minutes=interval_min)
        return out
    return Schedule(times, f"trading days every {interval_min}m {first:%H:%M}-close")


def manual() -> Schedule:
    """Never fires on its own: the job only runs when asked (CLI `run-job`)."""
    return Schedule(lambda d: [], "manual only")


def after_close(minutes: int) -> Schedule:
    """Trading days at close + minutes (16:20, or 13:20 on early-close days for minutes=20)."""
    def times(d: date) -> list[time]:
        if not is_trading_day(d):
            return []
        return [(datetime.combine(d, close_time(d)) + timedelta(minutes=minutes)).time()]
    return Schedule(times, f"trading days close+{minutes}m")


# ----------------------------------------------------------------------------- jobs
@dataclass(frozen=True)
class Job:
    name: str
    fn: Callable[..., Awaitable[str | None]]
    schedule: Schedule
    catch_up: bool = True


MON, SAT, SUN = 0, 5, 6


# Without a server-side ChainSource, closing chains are pushed by the IBKR bridge until EOD_DEADLINE:
# the EOD job (archive leftovers, daily bars, report snapshots, digest) runs just after it.
EOD_AFTER_PUSHES = (datetime.combine(date.min, EOD_DEADLINE) + timedelta(minutes=5)).time()


def build_jobs() -> dict[str, Job]:
    """All jobs (SPEC §5), keyed by name, in bootstrap order. Chain sweeps are scheduled only when a
    server-side ChainSource is configured (else `sweep` is manual-only and fails without a source).
    CBOE is not collected: `vix` (CBOE history CSVs) is manual-only."""
    s = get_settings()
    vendor = chain_source.configured() is not None
    jobs = [
        Job("universe", universe.universe, daily(time(6, 0), {MON})),
        Job("prices_backfill", prices.prices_backfill, daily(time(6, 10))),
        Job("prices_refresh", prices.prices_refresh, daily(time(7, 0), {SAT})),
        Job("splits", prices.splits, daily(time(6, 40))),
        Job("earnings", calendars.earnings, daily(time(5, 30))),
        Job("earnings_backfill", calendars.earnings_backfill, daily(time(4, 0))),  # no-op once complete
        Job("dividends", calendars.dividends, daily(time(5, 45))),
        Job("dividends_history", calendars.dividends_history, daily(time(6, 30), {SUN})),
        Job("vix", marketdata.vix, manual(), catch_up=False),
        Job("rates", marketdata.rates, daily(time(17, 5))),
        Job("sweep", chains.sweep, intraday(s.sweep_interval_min) if vendor else manual(), catch_up=vendor),
        Job("eod", chains.eod, after_close(FINAL_DELAY_MIN) if vendor   # final sweep sees the closing data
            else daily(EOD_AFTER_PUSHES, trading_only=True)),
        Job("prune", maintenance.prune, daily(time(3, 0))),
    ]
    return {j.name: j for j in jobs}


JOBS: dict[str, Job] = build_jobs()
_running: set[str] = set()


async def run_job(name: str, **kwargs: Any) -> tuple[bool, str | None]:
    """Run a job now (recorded in job_runs). Skips if that job is already running. Never raises
    for job failures; returns (ok, detail). Unknown name -> KeyError."""
    job = JOBS[name]
    if name in _running:
        log.info("job %s already running; skipped", name)
        return False, "already running"
    _running.add(name)
    rid = await asyncio.to_thread(jobs_repo.start_run, name)
    token = current_run_id.set(rid)
    ok, detail = True, None
    try:
        log.info("job %s started", name)
        detail = await job.fn(**kwargs)
    except asyncio.CancelledError:
        ok, detail = False, "cancelled"
        raise
    except Exception as e:  # noqa: BLE001 - a failing job must never kill the scheduler
        ok, detail = False, f"{type(e).__name__}: {e}"
        log.exception("job %s failed", name)
    finally:
        current_run_id.reset(token)
        _running.discard(name)
        await asyncio.shield(asyncio.to_thread(jobs_repo.finish_run, rid, ok, detail))
        log.info("job %s finished ok=%s %s", name, ok, detail)
    return ok, detail


def next_run_at(name: str, now: datetime | None = None) -> datetime | None:
    """Next scheduled fire time of a job (aware NY)."""
    return JOBS[name].schedule.next_after(now or now_ny())


def owed_run_at(job: Job, runs: list[dict], now: datetime) -> datetime | None:
    """When the job's latest scheduled slot (at or before now) is owed a run, given its recorded runs
    (newest first), or None. Owed = no successful run started since the slot: due now if nothing was
    attempted since, else RETRY_BACKOFF after the latest failed (or crashed) attempt, until MAX_ATTEMPTS
    attempts. Jobs without catch_up only ever run on schedule."""
    prev = job.schedule.prev_before(now)
    if prev is None or not job.catch_up:
        return None
    attempts = [r for r in runs if r["started_at"] >= prev]
    if any(r["ok"] for r in attempts) or len(attempts) >= MAX_ATTEMPTS:
        return None
    return max(now, attempts[0]["started_at"] + RETRY_BACKOFF) if attempts else now


async def _owed_run_at(job: Job, now: datetime) -> datetime | None:
    runs = await asyncio.to_thread(jobs_repo.recent_runs, MAX_ATTEMPTS + 1, job.name)
    return owed_run_at(job, runs, now)


async def missed(job: Job, now: datetime | None = None) -> bool:
    """True if the job's latest scheduled slot is owed a run due by now (see owed_run_at)."""
    now = now or now_ny()
    at = await _owed_run_at(job, now)
    return at is not None and at <= now


async def _sleep_until(ts: datetime) -> None:
    while (remaining := (ts - now_ny()).total_seconds()) > 0:
        await asyncio.sleep(min(remaining, MAX_SLEEP_S))


async def _loop(job: Job) -> None:
    """Run the job at its owed catch-up/retry time or its next scheduled slot, whichever comes first."""
    while True:
        now = now_ny()
        due = [t for t in (await _owed_run_at(job, now), job.schedule.next_after(now)) if t is not None]
        if not due:
            await asyncio.sleep(3600)
            continue
        await _sleep_until(min(due))
        await run_job(job.name)


_tasks: list[asyncio.Task] = []


async def _bootstrap() -> None:
    first = JOBS["universe"]
    if await missed(first):
        await run_job(first.name)
    for job in JOBS.values():
        _tasks.append(asyncio.create_task(_loop(job), name=f"job:{job.name}"))


async def start() -> None:
    """Start the scheduler in the running event loop (idempotent)."""
    if _tasks:
        return
    _tasks.append(asyncio.create_task(_bootstrap(), name="job:bootstrap"))
    log.info("scheduler started: %s", {j.name: j.schedule.label for j in JOBS.values()})


async def stop() -> None:
    """Cancel all job tasks and close the HTTP client."""
    tasks = list(_tasks)
    _tasks.clear()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await net.aclose()
