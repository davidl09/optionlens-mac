"""Option-chain pipeline: per-symbol refresh, the intraday sweep and the end-of-day archive."""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime, timedelta

import numpy as np

from ..analytics import chainmetrics, vol
from ..analytics.calendar import NY, is_market_open, last_completed_session, now_ny
from ..config import get_settings
from ..models import Bar, ChainSnapshot
from .. import net
from ..net import FetchError
from ..repo import bars as bars_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..sources import cboe
from . import hooks
from .marketdata import VIX_FAMILY
from ._util import final_since, report_progress, run_bounded, session_close, session_date

log = logging.getLogger(__name__)

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


def writes_metrics(day: date, now: datetime, final: bool) -> bool:
    """Whether a fetch at `now` may (over)write session `day`'s metrics_daily row: while the session is
    open and until its closing data has arrived (final_since), or in the EOD final sweep (`final`).
    Later fetches (evening, weekend, next pre-market) only refresh option_latest/quotes_latest: they
    never overwrite the finalized row, nor create one for a session that has none."""
    return final or now < final_since(day)


def store_chain(snap: ChainSnapshot, now: datetime, final: bool = False) -> None:
    """Persist a fetched chain: option_latest, quotes_latest, optionable flag and, per writes_metrics,
    the session's metrics row (time to expiry measured at the fetch, or at the close once it passed)."""
    symbol = snap.quote.symbol
    options_repo.replace_option_latest(snap)
    quotes_repo.upsert_quotes([snap.quote])
    symbols_repo.set_optionable(symbol, True)
    day = session_date(now)
    if writes_metrics(day, now, final):
        as_of = min(now, session_close(day))
        metrics_repo.upsert_metrics(day, symbol, chain_metrics_row(snap, day, as_of))


async def refresh_chain(symbol: str, final: bool = False) -> ChainSnapshot | None:
    """Fetch + store one symbol's chain (`final`: EOD sweep, see writes_metrics). None (and
    optionable=false) when CBOE lists no options."""
    try:
        snap = await cboe.fetch_chain(symbol)
    except FetchError as e:
        if e.status in NOT_LISTED and not e.blocked:
            await asyncio.to_thread(symbols_repo.set_optionable, symbol, False)
            return None
        raise
    if not snap.contracts:
        await asyncio.to_thread(quotes_repo.upsert_quotes, [snap.quote])
        await asyncio.to_thread(symbols_repo.set_optionable, symbol, False)
        return None
    await asyncio.to_thread(store_chain, snap, now_ny(), final)
    return snap


async def sweep(symbols: list[str] | None = None, final: bool = False) -> str:
    """Refresh chains for the universe (or given symbols), then fire on_sweep_complete.
    `final`: the EOD closing sweep (it writes the session's final metrics rows)."""
    if until := net.blocked_until(cboe.BASE_HOST):
        raise RuntimeError(f"CBOE requests suspended after a CDN block until "
                           f"{datetime.fromtimestamp(until, NY):%H:%M} ET; sweep skipped")
    syms = symbols or await asyncio.to_thread(symbols_repo.universe_symbols)
    t0 = _time.monotonic()
    done: list[str] = []
    skipped: set[str] = set()

    async def one(sym: str) -> None:
        if net.blocked_until(cboe.BASE_HOST):  # blocked mid-sweep: skip quietly, the job reports it
            skipped.add(sym)
            return
        if await refresh_chain(sym, final) is not None:
            done.append(sym)
        if len(done) % 100 == 0 and done:
            await report_progress(f"{len(done)}/{len(syms)} chains")

    ok, failed = await run_bounded(syms, one, get_settings().cboe_concurrency, "sweep")
    if failed:  # second pass for transient failures (e.g. a rate-limit burst)
        ok2, failed = await run_bounded(failed, one, get_settings().cboe_concurrency, "sweep-retry")
        ok += ok2
    blocked = f"{len(skipped)} skipped (CDN block), " if skipped else ""
    detail = (f"{len(done)} chains, {ok - len(done) - len(skipped)} not optionable, {blocked}"
              f"{len(failed)} failed, {_time.monotonic() - t0:.0f}s")
    if failed:
        detail += f" (failed: {','.join(failed[:20])})"
    if net.blocked_until(cboe.BASE_HOST):
        raise RuntimeError(f"CBOE refused requests (CDN block) mid-sweep; partial: {detail}")
    await refresh_index_quotes(VIX_FAMILY)
    await hooks.fire(hooks.on_sweep_complete, session_date(now_ny()), sorted(done))
    return detail


async def refresh_index_quotes(symbols: tuple[str, ...]) -> None:
    """Live quotes for quote-only indices (the VIX family has no listed chain here) -> quotes_latest."""
    quotes = []
    for sym in symbols:
        try:
            quotes.append(await cboe.fetch_quote(sym))
        except FetchError as e:
            log.warning("index quote %s failed: %s", sym, e)
    if quotes:
        await asyncio.to_thread(quotes_repo.upsert_quotes, quotes)


def eod_day(now: datetime | None = None) -> date:
    """Session the EOD archive is for. ValueError while a session is open or before its closing data
    has arrived (final_since): the archive would hold intraday data."""
    now = now or now_ny()
    if is_market_open(now):
        raise ValueError("market is open: EOD can only run after the close")
    day = last_completed_session(now)
    if now < final_since(day):
        raise ValueError(f"EOD for {day} can only run from {final_since(day):%H:%M} (delayed closing data)")
    return day


def store_quote_bars(day: date, symbols: list[str] | None) -> list[Bar]:
    """daily_bars for `day` from stored quotes of chains fetched at/after final_since(day) (the closing
    snapshot; quotes without a chain carry no fetch time and are skipped). Universe symbols keep any
    stored bar (Nasdaq backfill/refresh owns their history); other symbols' bars are replaced, so an
    earlier non-final bar is corrected. Returns the bars offered."""
    final = options_repo.fetched_since(final_since(day), symbols)
    universe = set(symbols_repo.universe_symbols(include_unoptionable=True))
    out = [Bar(sym, day, q.open, q.high, q.low, q.price, q.volume or None)
           for sym, q in quotes_repo.all_quotes().items() if sym in final and q.price]
    bars_repo.upsert_bars([b for b in out if b.symbol in universe], replace=False)
    bars_repo.upsert_bars([b for b in out if b.symbol not in universe])
    return out


async def eod(symbols: list[str] | None = None) -> str:
    """Final sweep, then option_eod + daily_bars (from CBOE quotes) for the session, then on_eod hooks.
    Without `symbols`, archives every chain fetched since final_since (universe and on-demand symbols)."""
    day = eod_day()
    sweep_detail = await sweep(symbols, final=True)
    n_eod = await asyncio.to_thread(options_repo.write_option_eod, day, final_since(day), symbols)
    bars = await asyncio.to_thread(store_quote_bars, day, symbols)
    await hooks.fire(hooks.on_eod, day, sorted({b.symbol for b in bars}))
    return f"{day}: {n_eod} option_eod rows, {len(bars)} bars; sweep: {sweep_detail}"
