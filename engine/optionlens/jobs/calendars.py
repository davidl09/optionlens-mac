"""Earnings and dividend calendars: rolling windows, resumable 3-year earnings backfills, weekly per-symbol dividend
histories. US listings from Nasdaq; Canadian listings' earnings from TMX Money, else Yahoo Finance
(sources/ca_earnings.py, docs/INTEGRATION.md §12 "Earnings sources"), and their dividend histories from TMX Money
(dividends_ca, §12.2)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Awaitable, Callable

from ..analytics import markets
from ..analytics.calendar import NY, now_ny
from ..config import get_settings
from ..models import EarningsEvent
from ..repo import dividends as dividends_repo
from ..repo import earnings as earnings_repo
from ..repo import jobs as jobs_repo
from ..repo import symbols as symbols_repo
from ..net import FetchError
from ..sources import ca_earnings, nasdaq, tmx
from ._util import report_progress, run_bounded
from . import ondemand

EARNINGS_BACK, EARNINGS_FWD = 10, 90
DIVIDENDS_BACK, DIVIDENDS_FWD = 10, 60
BACKFILL_YEARS = 3
BACKFILL_JOB = "earnings_backfill"
CA_JOB, CA_BACKFILL_JOB = "earnings_ca", "earnings_backfill_ca"
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


# ----------------------------------------------------------------------------- Canadian listings' earnings
def _ca_symbols() -> tuple[list[str], Callable[[str], str]]:
    """(the Canadian stocks to look up: every known Canadian listing of kind stock, the universe first; a TMX ticker
    -> its canonical symbol: the known listing with that ticker, else the TSX one)."""
    ca = [r for r in symbols_repo.list_symbols() if markets.is_canadian(r["symbol"])]
    known = {markets.ticker(r["symbol"]): r["symbol"] for r in ca}
    stocks = [r["symbol"] for r in sorted(ca, key=lambda r: not r["in_universe"]) if r["kind"] == "stock"]
    return stocks, lambda t: known.get(t) or markets.canadian_symbol(t, "TSX")


NO_CA = "no Canadian listings known (they come with Questrade: Settings → Data sources)"


def _health(state: str | None) -> str:
    """A source's state in a run's detail: "ok", or why it was unavailable (braces dropped: the detail ends with this
    JSON object, which ca_source_health reads back)."""
    return "ok" if state is None else state.replace("{", "(").replace("}", ")")


async def earnings_ca(start: date | None = None, end: date | None = None) -> str:
    """Canadian listings' earnings: TMX Money's calendar per weekday of [today-10, today+90] (or [start, end]; replaces
    TMX's rows of each non-empty day), then each Canadian stock's next date from the first source that has one
    (TMX Money, else Yahoo Finance; symbols answered earlier today are skipped). A failing TMX request makes TMX
    unavailable for the run. The detail ends with each source's health ({"tmx": "ok", "yahoo": "not needed"}).
    Fails only when no source could be reached."""
    now = now_ny()
    today = now.date()
    stocks, to_symbol = await asyncio.to_thread(_ca_symbols)
    if not stocks:
        return NO_CA
    days = days_between(start or today - timedelta(days=EARNINGS_BACK), end or today + timedelta(days=EARNINGS_FWD),
                        weekdays_only=True)
    stored, fetched, tmx_down = 0, 0, None
    calendar_next: dict[str, EarningsEvent] = {}
    for d in days:
        try:
            events = await tmx.fetch_earnings_day(d, to_symbol)
        except FetchError as e:
            tmx_down = str(e)
            break
        fetched += 1
        if events:  # an empty answer may be a glitch: never wipe a stored day with it
            stored += await asyncio.to_thread(earnings_repo.replace_day, d, events, tmx.NAME)
        for e in events:
            if e.date >= today and (e.symbol not in calendar_next or e.date < calendar_next[e.symbol].date):
                calendar_next[e.symbol] = e

    async def tmx_upcoming(symbols, on):   # the per-company dates, else the calendar's (same source)
        got, err = await tmx.fetch_upcoming(symbols, on)
        return {**{s: calendar_next[s] for s in symbols if s in calendar_next}, **got}, err

    since = datetime.combine(today, time(0), NY).astimezone(timezone.utc).replace(tzinfo=None)
    done = await asyncio.to_thread(earnings_repo.answered_since, since)
    todo = [s for s in stocks if s not in done]
    sel = await ca_earnings.select_upcoming(todo, today, down={tmx.NAME: tmx_down} if tmx_down else None,
                                            fetchers={tmx.NAME: tmx_upcoming})
    await asyncio.to_thread(earnings_repo.replace_upcoming, sel.events, today + timedelta(days=1),
                            ca_earnings.PRECEDENCE)
    checked = datetime.now(timezone.utc).replace(tzinfo=None)
    why = sel.why_none()
    await asyncio.to_thread(earnings_repo.record_lookups,
                            [(s, checked, ev.source, None) for s, ev in sel.events.items()]
                            + [(s, checked, None, why) for s in sel.unanswered])
    health = {tmx.NAME: _health(tmx_down or sel.health.get(tmx.NAME)),
              **{n: _health(sel.health[n]) if n in sel.health else "not needed" for n in ca_earnings.PRECEDENCE[1:]}}
    c = sel.counts()
    detail = (f"TMX calendar: {stored} events over {fetched}/{len(days)} days; next dates of {len(todo)} listings: "
              + ", ".join(f"{k} {v}" for k, v in c.items())
              + (f" ({len(stocks) - len(todo)} looked up earlier today)" if len(todo) < len(stocks) else "")
              + " " + json.dumps(health))
    if todo and not sel.events and all(health[n] not in ("ok", "not needed") for n in health):
        raise RuntimeError(detail)    # no source could be reached
    return detail


def ca_source_health(runs: int = 30) -> dict[str, dict]:
    """{source: {state ("ok" | why unavailable | "not needed" | None: no run yet), at, last_success_at}} of the
    Canadian earnings sources, from the latest earnings_ca runs' details."""
    out = {n: {"state": None, "at": None, "last_success_at": None} for n in ca_earnings.PRECEDENCE}
    for run in jobs_repo.recent_runs(limit=runs, job=CA_JOB):
        detail = run["detail"] or ""
        try:
            health = json.loads(detail[detail.rindex("{"):])
        except (ValueError, TypeError):
            continue
        if not isinstance(health, dict):
            continue
        for n, h in out.items():
            st = health.get(n)
            if h["at"] is None and isinstance(st, str):
                h["state"], h["at"] = st, run["finished_at"] or run["started_at"]
            if h["last_success_at"] is None and st == "ok":
                h["last_success_at"] = run["finished_at"] or run["started_at"]
    return out


async def earnings_backfill_ca() -> str:
    """TMX Money's earnings calendar (Canadian listings), 3 years back (resumable; nothing to do without any)."""
    stocks, to_symbol = await asyncio.to_thread(_ca_symbols)
    if not stocks:
        return NO_CA
    return await _backfill(CA_BACKFILL_JOB, lambda d: tmx.fetch_earnings_day(d, to_symbol))


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


async def dividends_ca() -> str:
    """TMX Money's dividend history of every known Canadian stock and ETF, the universe first (one request each; a
    listing answered within ondemand's cache time, e.g. opened by the user today, is not asked again). A failing
    request makes TMX unavailable for the rest of the run: what was stored stays. Fails when the first request
    does."""
    rows = [r for r in await asyncio.to_thread(symbols_repo.list_symbols)
            if markets.is_canadian(r["symbol"]) and r["kind"] != "index"]
    symbols = [r["symbol"] for r in sorted(rows, key=lambda r: not r["in_universe"])]
    if not symbols:
        return NO_CA
    done = paying = other = 0
    for i, s in enumerate(symbols):
        try:
            got = await ondemand.fetch_cached("dividends", s, stale_ok=False)
        except FetchError as e:
            if not done:
                raise
            return f"{paying} of {done} listings with dividends; TMX Money unavailable after {done}/{len(symbols)}: {e}"
        done += 1
        paying += bool(got["dividends"])
        other += bool(got["other_currency"]) and not got["dividends"]
        if (i + 1) % PROGRESS_EVERY == 0:
            await report_progress(f"{done}/{len(symbols)} listings")
    return (f"{paying} of {done} listings with dividends"
            + (f"; {other} pay in another currency (not stored)" if other else ""))
