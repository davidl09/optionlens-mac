"""Daily price history: 10y backfill (resumable), catch-up of stale symbols, weekly 30-day refresh.

Stocks/ETFs come from Nasdaq. Indices come from CBOE daily_prices CSVs (SPX and RUT close-only,
XSP OHLC; NDX is not published -> its history starts with the first EOD run).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import numpy as np

from ..analytics.calendar import last_completed_session, now_ny
from ..config import get_settings
from ..models import Bar
from ..net import FetchError
from ..repo import bars as bars_repo
from ..repo import symbols as symbols_repo
from ..sources import cboe, nasdaq
from ._util import log, run_bounded

HISTORY_YEARS = 10
REFRESH_DAYS = 30
SPLIT_TOLERANCE = 0.02   # median |stored/new - 1| above this => adjusted history => refetch all
CATCH_UP_OVERLAP_DAYS = 7


def _asset(kind: str) -> nasdaq.AssetClass:
    return "etf" if kind == "etf" else "stocks"


async def fetch_bars(symbol: str, kind: str, start: date, end: date) -> list[Bar]:
    """Daily bars in [start, end] from the right source for the symbol kind ([] if unavailable)."""
    if kind == "index":
        try:
            bars = await cboe.fetch_history(symbol)
        except FetchError as e:
            log.info("no CBOE history for %s (%s)", symbol, e.status)
            return []
        return [b for b in bars if start <= b.date <= end]
    return await nasdaq.fetch_history(symbol, _asset(kind), start, end)


def plan_backfill(symbols: list[dict], coverage: dict[str, dict], today: date, last_session: date) -> list[tuple[dict, date]]:
    """(symbol row, fetch start) for symbols without history (10y) or whose last bar predates last_session."""
    plan = []
    for row in symbols:
        cov = coverage.get(row["symbol"])
        if cov is None:
            plan.append((row, today - timedelta(days=365 * HISTORY_YEARS + 3)))
        elif cov["last"] < last_session:
            plan.append((row, cov["last"] - timedelta(days=CATCH_UP_OVERLAP_DAYS)))
    return plan


async def backfill_symbol(row: dict, start: date | None = None) -> int:
    """Fetch and store bars for one symbols row from start (default 10y). Returns bars stored."""
    today = now_ny().date()
    start = start or today - timedelta(days=365 * HISTORY_YEARS + 3)
    bars = await fetch_bars(row["symbol"], row["kind"], start, today)
    return await asyncio.to_thread(bars_repo.upsert_bars, bars) if bars else 0


async def prices_backfill(symbols: list[str] | None = None, limit: int | None = None) -> str:
    """Backfill universe (or given) symbols lacking history, and catch up stale ones."""
    def load() -> tuple[list[dict], dict]:
        if symbols:
            rows = [r for r in (symbols_repo.get_symbol(s) for s in symbols) if r]
        else:
            rows = symbols_repo.list_symbols(in_universe=True)
        return rows, bars_repo.coverage([r["symbol"] for r in rows])

    rows, cov = await asyncio.to_thread(load)
    now = now_ny()
    plan = plan_backfill(rows, cov, now.date(), last_completed_session(now))
    if limit:
        plan = plan[:limit]
    stored = 0

    async def one(item: tuple[dict, date]) -> None:
        nonlocal stored
        n = await backfill_symbol(*item)
        stored += n

    ok, failed = await run_bounded(plan, one, max(1, int(get_settings().nasdaq_rps)), "backfill")
    return f"{ok}/{len(plan)} symbols, {stored} bars" + (f", failed {[r['symbol'] for r, _ in failed][:20]}" if failed else "")


def adjusted_since_stored(stored: dict[str, np.ndarray], new: list[Bar]) -> bool:
    """True when fresh closes disagree with stored ones on overlapping dates (split/dividend adjustment)."""
    if not new or len(stored["date"]) == 0:
        return False
    by_date = {np.datetime64(b.date): b.close for b in new}
    ratios = [c / by_date[d] - 1 for d, c in zip(stored["date"], stored["close"]) if d in by_date and by_date[d]]
    return bool(ratios) and float(np.median(np.abs(ratios))) > SPLIT_TOLERANCE


async def refresh_symbol(row: dict) -> str:
    """Refetch the last 30 days; on an adjustment mismatch refetch and replace the full history."""
    today = now_ny().date()
    start = today - timedelta(days=REFRESH_DAYS)
    new = await fetch_bars(row["symbol"], row["kind"], start, today)
    stored = await asyncio.to_thread(bars_repo.get_bars, row["symbol"], start, today)
    if adjusted_since_stored(stored, new):
        full = await fetch_bars(row["symbol"], row["kind"], today - timedelta(days=365 * HISTORY_YEARS + 3), today)
        await asyncio.to_thread(bars_repo.replace_bars, row["symbol"], full)
        return "replaced"
    if new:
        await asyncio.to_thread(bars_repo.upsert_bars, new)
    return "refreshed"


async def prices_refresh(symbols: list[str] | None = None) -> str:
    """Weekly: refetch the last 30 days for all universe symbols (corrects revisions, detects splits)."""
    rows = await asyncio.to_thread(symbols_repo.list_symbols, in_universe=True)
    if symbols:
        rows = [r for r in rows if r["symbol"] in symbols]
    replaced: list[str] = []

    async def one(row: dict) -> None:
        if await refresh_symbol(row) == "replaced":
            replaced.append(row["symbol"])

    ok, failed = await run_bounded(rows, one, max(1, int(get_settings().nasdaq_rps)), "refresh")
    return f"{ok}/{len(rows)} refreshed, full refetch: {replaced[:50]}, failed: {len(failed)}"
