"""Daily price history: 10y backfill (resumable), catch-up of stale symbols, weekly 30-day refresh.

US stocks/ETFs come from Nasdaq, Canadian listings from Questrade's daily candles (Nasdaq does not cover them; skipped
with an error while Questrade is not connected). Indices have no source here (CBOE is not collected): their bars come
from the IBKR bridge's pushes and the EOD job's stored quotes.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import numpy as np

from ..analytics import markets
from ..analytics.calendar import now_ny
from ..analytics.splits import shows_unadjusted_split
from ..config import get_settings
from ..models import Bar
from ..repo import bars as bars_repo
from ..repo import symbols as symbols_repo
from ..net import FetchError
from ..sources import nasdaq
from . import universe_ca
from ._util import run_bounded

HISTORY_YEARS = 10
REFRESH_DAYS = 30
SPLIT_TOLERANCE = 0.02   # median |stored/new - 1| above this => adjusted history => refetch all
CATCH_UP_OVERLAP_DAYS = 7
COVER_SLACK_DAYS = 7     # a full refetch may start this much after the expected first date (holidays, listing)


def history_start(today: date) -> date:
    """First date of a full (10y) history fetch."""
    return today - timedelta(days=365 * HISTORY_YEARS + 3)


def _asset(kind: str) -> nasdaq.AssetClass:
    return "etf" if kind == "etf" else "stocks"


async def fetch_bars(symbol: str, kind: str, start: date, end: date) -> list[Bar]:
    """Daily bars in [start, end] from the right source for the symbol ([] if unavailable): Questrade for Canadian
    listings (FetchError while it is not connected), Nasdaq for US stocks / ETFs, none for indices."""
    if kind == "index":
        return []
    if markets.is_canadian(symbol):
        src = universe_ca.directory()
        if src is None:
            raise FetchError(f"questrade:candles/{symbol}", None, "Questrade is not connected (Canadian price history)")
        return await src.fetch_daily(symbol, start, end)
    return await nasdaq.fetch_history(symbol, _asset(kind), start, end)


def store_bars(bars: list[Bar]) -> int:
    """Upsert fetched bars. Close-only bars (no open/high/low, e.g. CBOE SPX/RUT) only set the close, so
    OHLC built by the EOD job is kept. Returns rows offered."""
    close_only = [b.open is None and b.high is None and b.low is None for b in bars]
    return (bars_repo.upsert_closes([b for b, c in zip(bars, close_only) if c])
            + bars_repo.upsert_bars([b for b, c in zip(bars, close_only) if not c]))


THIN_HISTORY_BARS = 60   # fewer stored bars than this: the history was never fetched (or the listing is this new)


def lacks_history(cov: dict | None) -> bool:
    """No stored bars, or only the few stored before a history was ever fetched (see plan_backfill)."""
    return cov is None or cov["count"] < THIN_HISTORY_BARS


def plan_backfill(symbols: list[dict], coverage: dict[str, dict], today: date, last_session: date) -> list[tuple[dict, date]]:
    """(symbol row, fetch start) for symbols without a history (10y) or whose last bar predates last_session. A
    symbol with only a few bars has no history either: the EOD job or a source's daily bar can store a symbol's first
    bars before its backfill runs (a listing new to the universe), and those must not pass for a fetched history.
    A listing younger than THIN_HISTORY_BARS sessions is asked in full each day until it has that many (one request)."""
    plan = []
    for row in symbols:
        cov = coverage.get(row["symbol"])
        if lacks_history(cov):
            plan.append((row, history_start(today)))
        elif cov["last"] < last_session:
            plan.append((row, cov["last"] - timedelta(days=CATCH_UP_OVERLAP_DAYS)))
    return plan


async def backfill_symbol(row: dict, start: date | None = None) -> int:
    """Fetch and store bars for one symbols row from start (default 10y). Returns bars stored."""
    today = now_ny().date()
    start = start or history_start(today)
    bars = await fetch_bars(row["symbol"], row["kind"], start, today)
    return await asyncio.to_thread(store_bars, bars) if bars else 0


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
    by_sym = {r["symbol"]: r for r in rows}
    plan = [item for m, syms in markets.by_market(list(by_sym)).items()
            for item in plan_backfill([by_sym[s] for s in syms], cov, now.date(), m.calendar.last_completed_session(now))]
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


def covers_history(full: list[Bar], stored_first: date, today: date) -> bool:
    """True when a full refetch reaches back to the stored history's start (or, for histories longer
    than a fresh backfill, to the backfill start), within COVER_SLACK_DAYS."""
    expected = max(stored_first, history_start(today))
    return bool(full) and min(b.date for b in full) <= expected + timedelta(days=COVER_SLACK_DAYS)


async def refresh_symbol(row: dict) -> str:
    """Refetch the last 30 days; on an adjustment mismatch refetch and replace the full history.
    ValueError (stored history kept) when that refetch is empty or does not cover the stored history."""
    sym, today = row["symbol"], now_ny().date()
    start = today - timedelta(days=REFRESH_DAYS)
    new = await fetch_bars(sym, row["kind"], start, today)
    stored = await asyncio.to_thread(bars_repo.get_bars, sym, start, today)
    if adjusted_since_stored(stored, new):
        first = (await asyncio.to_thread(bars_repo.coverage, [sym]))[sym]["first"]
        full = await fetch_bars(sym, row["kind"], min(first, history_start(today)), today)
        if not covers_history(full, first, today):
            got = f"from {min(b.date for b in full)}" if full else "no bars"
            raise ValueError(f"{sym}: split refetch returned {got}, stored history starts {first}; kept")
        await asyncio.to_thread(bars_repo.replace_bars, sym, full)
        return "replaced"
    if new:
        await asyncio.to_thread(store_bars, new)
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


SPLITS_LOOKBACK_DAYS = 30
SPLIT_WINDOW_DAYS = 10   # calendar days of bars read on each side of an ex-date


def apply_splits(splits: list[dict], today: date) -> list[str]:
    """Split-adjust stored daily bars for executed splits ({symbol, execution_date, split_factor}) whose stored
    bars still show the unadjusted jump at the ex-date (idempotent: adjusted history is left alone; a later Nasdaq
    refetch of adjusted history is not adjusted again). Returns 'SYM ratio ex-date' of the splits applied."""
    done = []
    for sp in splits:
        ex, f = date.fromisoformat(str(sp["execution_date"])), sp.get("split_factor")
        if ex > today or not f:
            continue
        b = bars_repo.get_bars(sp["symbol"], ex - timedelta(days=SPLIT_WINDOW_DAYS), ex + timedelta(days=SPLIT_WINDOW_DAYS))
        dates = b["date"].astype("datetime64[D]").astype(object).tolist()
        before = [c for d, c in zip(dates, b["close"].tolist()) if d < ex]
        on = [c for d, c in zip(dates, b["close"].tolist()) if d >= ex]
        if before and on and shows_unadjusted_split(before[-1], on[0], f):
            bars_repo.scale_before(sp["symbol"], ex, f)
            done.append(f"{sp['symbol']} {sp.get('ratio') or f} {ex}")
    return done


async def splits() -> str:
    """Daily: the Nasdaq splits calendar of the last 30 days -> split-adjust stored bars that are not adjusted yet
    (Nasdaq's history is adjusted, but bars stored before a split stay unadjusted until a refetch)."""
    today = now_ny().date()
    rows = await nasdaq.fetch_splits(today - timedelta(days=SPLITS_LOOKBACK_DAYS))
    done = await asyncio.to_thread(apply_splits, rows, today)
    return f"{len(rows)} splits listed, adjusted: {done[:50]}"

