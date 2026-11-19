"""Option-chain pipeline: per-symbol refresh, the intraday sweep and the end-of-day archive."""
from __future__ import annotations

import asyncio
import time as _time
from datetime import date, datetime, timedelta

import numpy as np

from ..analytics import chainmetrics, vol
from ..analytics.calendar import NY, close_time, is_market_open, last_completed_session, now_ny
from ..config import get_settings
from ..models import Bar, ChainSnapshot
from ..net import FetchError
from ..repo import bars as bars_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..sources import cboe
from . import hooks
from ._util import report_progress, run_bounded, session_date

HV_LOOKBACK_DAYS = 400  # calendar days of closes loaded for HV252
NOT_LISTED = (403, 404)  # CBOE answers these for symbols without listed options


def chain_metrics_row(snap: ChainSnapshot, day: date, now: datetime) -> dict:
    """metrics_daily values for `day` from a chain + stored closes (today's live price appended if
    today's bar is not stored yet)."""
    q = snap.quote
    b = bars_repo.get_bars(q.symbol, start=day - timedelta(days=HV_LOOKBACK_DAYS), end=day)
    closes = b["close"]
    if q.price and (len(b["date"]) == 0 or b["date"][-1] < np.datetime64(day)):
        closes = np.append(closes, q.price)
    return {
        **chainmetrics.compute_chain_metrics(snap, now),
        **vol.hv_snapshot(closes),
        "close": q.price, "change_pct": q.change_pct, "volume": q.volume,
    }


def store_chain(snap: ChainSnapshot, now: datetime) -> None:
    """Persist a fetched chain: option_latest, quotes_latest, optionable flag and today's metrics row."""
    symbol = snap.quote.symbol
    options_repo.replace_option_latest(snap)
    quotes_repo.upsert_quotes([snap.quote])
    symbols_repo.set_optionable(symbol, True)
    day = session_date(now)
    metrics_repo.upsert_metrics(day, symbol, chain_metrics_row(snap, day, now))


async def refresh_chain(symbol: str) -> ChainSnapshot | None:
    """Fetch + store one symbol's chain. None (and optionable=false) when CBOE lists no options."""
    try:
        snap = await cboe.fetch_chain(symbol)
    except FetchError as e:
        if e.status in NOT_LISTED:
            await asyncio.to_thread(symbols_repo.set_optionable, symbol, False)
            return None
        raise
    if not snap.contracts:
        await asyncio.to_thread(quotes_repo.upsert_quotes, [snap.quote])
        await asyncio.to_thread(symbols_repo.set_optionable, symbol, False)
        return None
    await asyncio.to_thread(store_chain, snap, now_ny())
    return snap


async def sweep(symbols: list[str] | None = None) -> str:
    """Refresh chains for the universe (or given symbols), then fire on_sweep_complete."""
    syms = symbols or await asyncio.to_thread(symbols_repo.universe_symbols)
    t0 = _time.monotonic()
    done: list[str] = []

    async def one(sym: str) -> None:
        if await refresh_chain(sym) is not None:
            done.append(sym)
        if len(done) % 100 == 0 and done:
            await report_progress(f"{len(done)}/{len(syms)} chains")

    ok, failed = await run_bounded(syms, one, get_settings().cboe_concurrency, "sweep")
    if failed:  # second pass for transient failures (e.g. a rate-limit burst)
        ok2, failed = await run_bounded(failed, one, get_settings().cboe_concurrency, "sweep-retry")
        ok += ok2
    detail = (f"{len(done)} chains, {ok - len(done)} not optionable, {len(failed)} failed, "
              f"{_time.monotonic() - t0:.0f}s")
    if failed:
        detail += f" (failed: {','.join(failed[:20])})"
    await hooks.fire(hooks.on_sweep_complete, session_date(now_ny()), sorted(done))
    return detail


def eod_day(now: datetime | None = None) -> date:
    """Session the EOD archive is for; ValueError while a session is open (data would be intraday)."""
    now = now or now_ny()
    if is_market_open(now):
        raise ValueError("market is open: EOD can only run after the close")
    return last_completed_session(now)


def _quote_bars(day: date, symbols: list[str] | None) -> list[Bar]:
    """Daily bars for `day` from stored quotes whose as-of date is that day."""
    out = []
    for sym, q in quotes_repo.all_quotes().items():
        if (symbols is None or sym in symbols) and q.price and q.ts.astimezone(NY).date() == day:
            out.append(Bar(sym, day, q.open, q.high, q.low, q.price, q.volume or None))
    return out


async def eod(symbols: list[str] | None = None) -> str:
    """Final sweep, then option_eod + daily_bars (from CBOE quotes) for the session, then on_eod hooks.
    Without `symbols`, archives every chain fetched after the close (universe and on-demand symbols);
    existing Nasdaq bars are never overwritten by quote-derived ones."""
    day = eod_day()
    sweep_detail = await sweep(symbols)
    close_dt = datetime.combine(day, close_time(day), NY)
    n_eod = await asyncio.to_thread(options_repo.write_option_eod, day, close_dt, symbols)
    bars = await asyncio.to_thread(_quote_bars, day, symbols)
    n_bars = await asyncio.to_thread(bars_repo.upsert_bars, bars, False)
    await hooks.fire(hooks.on_eod, day, sorted({b.symbol for b in bars}))
    return f"{day}: {n_eod} option_eod rows, {n_bars} bars; sweep: {sweep_detail}"
