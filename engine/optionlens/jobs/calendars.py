"""Earnings and dividend calendars (Nasdaq): rolling windows, resumable 3-year earnings backfill,
weekly per-symbol dividend histories."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Awaitable, Callable

from ..analytics.calendar import now_ny
from ..config import get_settings
from ..models import EarningsEvent
from ..repo import dividends as dividends_repo
from ..repo import earnings as earnings_repo
from ..repo import jobs as jobs_repo
from ..repo import symbols as symbols_repo
from ..sources import nasdaq
from ._util import report_progress, run_bounded
from . import ondemand

EARNINGS_BACK, EARNINGS_FWD = 10, 90
DIVIDENDS_BACK, DIVIDENDS_FWD = 10, 60
BACKFILL_YEARS = 3
BACKFILL_JOB = "earnings_backfill"
PROGRESS_EVERY = 20


def days_between(start: date, end: date, weekdays_only: bool = False) -> list[date]:
    """Inclusive calendar days in [start, end] (optionally Mon-Fri only)."""
    out, d = [], start
    while d <= end:
        if not weekdays_only or d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _rps() -> int:
    return max(1, int(get_settings().nasdaq_rps))


async def earnings(start: date | None = None, end: date | None = None) -> str:
    """Fetch the earnings calendar per day for [today-10, today+90] (or [start, end]); replaces each
    non-empty day so rescheduled events disappear."""
    today = now_ny().date()
    days = days_between(start or today - timedelta(days=EARNINGS_BACK), end or today + timedelta(days=EARNINGS_FWD))
    total = 0

    async def one(d: date) -> None:
        nonlocal total
        events = await nasdaq.fetch_earnings(d)
        if events:  # an empty answer may be a glitch: never wipe a stored day with it
            n = await asyncio.to_thread(earnings_repo.replace_day, d, events, nasdaq.NAME)
            total += n

    ok, failed = await run_bounded(days, one, _rps(), "earnings")
    return f"{total} events over {ok}/{len(days)} days" + (f", failed {[str(d) for d in failed]}" if failed else "")


def _backfill_window(today: date) -> tuple[date, date]:
    """Weekday range covered by the backfill (the rolling job covers the last EARNINGS_BACK days)."""
    return today - timedelta(days=365 * BACKFILL_YEARS), today - timedelta(days=EARNINGS_BACK + 1)


class BackfillInterrupted(RuntimeError):
    """Raised with the JSON progress as message so a failed run still records its cursor."""


def _backfill_cursor(job: str = BACKFILL_JOB) -> date | None:
    """Oldest date already backfilled (from the latest run of `job` that recorded one)."""
    for run in jobs_repo.recent_runs(limit=20, job=job):
        detail = run["detail"] or ""
        try:
            return date.fromisoformat(json.loads(detail[detail.index("{"):])["cursor"])
        except (ValueError, KeyError, TypeError):
            continue
    return None


def earnings_backfill_progress(today: date | None = None, job: str = BACKFILL_JOB) -> float:
    """Fraction 0..1 of the 3-year earnings backfill `job` completed (for Status)."""
    today = today or now_ny().date()
    start, end = _backfill_window(today)
    cursor = _backfill_cursor(job)
    if cursor is None:
        return 0.0
    days = days_between(start, end, weekdays_only=True)
    done = sum(1 for d in days if d >= cursor)
    return min(1.0, done / len(days)) if days else 1.0


async def _backfill(job: str, fetch_day: Callable[[date], Awaitable[list[EarningsEvent]]]) -> str:
    """Resumable: fetch every weekday from `job`'s cursor back to 3 years ago (newest first) and upsert it."""
    today = now_ny().date()
    start, end = _backfill_window(today)
    cursor = await asyncio.to_thread(_backfill_cursor, job)
    todo = [d for d in reversed(days_between(start, end, weekdays_only=True)) if cursor is None or d < cursor]
    total, done = 0, cursor or end + timedelta(days=1)

    def progress() -> str:
        return json.dumps({"cursor": done.isoformat(), "events": total})

    for i, d in enumerate(todo, 1):
        try:
            n = await asyncio.to_thread(earnings_repo.upsert_earnings, await fetch_day(d))
            total += n
        except Exception as e:
            raise BackfillInterrupted(f"{type(e).__name__}: {e} {progress()}") from e
        done = d
        if i % PROGRESS_EVERY == 0:
            await report_progress(progress())
    return progress()


async def earnings_backfill() -> str:
    """Nasdaq's earnings calendar (US listings), 3 years back (resumable)."""
    return await _backfill(BACKFILL_JOB, nasdaq.fetch_earnings)


async def dividends(start: date | None = None, end: date | None = None) -> str:
    """Fetch the dividend calendar per day for [today-10, today+60] (or [start, end])."""
    today = now_ny().date()
    days = days_between(start or today - timedelta(days=DIVIDENDS_BACK), end or today + timedelta(days=DIVIDENDS_FWD))
    total = 0

    async def one(d: date) -> None:
        nonlocal total
        n = await asyncio.to_thread(dividends_repo.upsert_dividends, await nasdaq.fetch_dividend_calendar(d))
        total += n

    ok, failed = await run_bounded(days, one, _rps(), "dividends")
    return f"{total} dividends over {ok}/{len(days)} days"


async def dividends_history(symbols: list[str] | None = None) -> str:
    """Per-symbol dividend history for universe stocks/ETFs (or given symbols)."""
    rows = await asyncio.to_thread(symbols_repo.list_symbols, in_universe=True)
    rows = [r for r in rows if r["kind"] != "index" and ondemand.covered_by_nasdaq(r["symbol"])
            and (not symbols or r["symbol"] in symbols)]
    total = 0

    async def one(row: dict) -> None:
        nonlocal total
        asset = "etf" if row["kind"] == "etf" else "stocks"
        divs = await nasdaq.fetch_dividend_history(row["symbol"], asset)
        n = await asyncio.to_thread(dividends_repo.upsert_dividends, divs)
        total += n

    ok, failed = await run_bounded(rows, one, _rps(), "dividend history")
    return f"{total} dividends for {ok}/{len(rows)} symbols"
