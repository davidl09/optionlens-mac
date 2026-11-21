"""Option-chain pipeline: storing chains from any source (IBKR ingest, an optional vendor ChainSource),
the vendor sweep (only when a ChainSource is configured) and the end-of-day archive.

Precedence for one session (repo.options.outranks): an IBKR chain is never replaced by another
source's in metrics_daily / option_eod; option_latest always holds the newest chain with its source.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime, timedelta
from typing import Mapping, Sequence

import numpy as np

from ..analytics import chainmetrics, vol
from ..analytics.calendar import is_market_open, last_completed_session, now_ny
from ..models import Bar, ChainSnapshot
from ..repo import bars as bars_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..repo import vol_history as vol_history_repo
from ..sources import chain_source
from ..sources.chain_source import SOURCE_TAG, ChainSource
from . import hooks
from ._util import final_since, report_progress, run_bounded, session_close, session_date

log = logging.getLogger(__name__)

HV_LOOKBACK_DAYS = 400  # calendar days of closes loaded for HV252
SWEEP_CONCURRENCY = 4
STATS_SUFFIX = ":stats"  # metrics_daily.source of a row holding only pushed underlying stats


def closes_with_live(stored: dict[str, np.ndarray], day: date, price: float | None) -> np.ndarray:
    """Stored closes through `day`, with today's live price appended if `day`'s bar is not stored yet."""
    closes = stored["close"]
    if price and (len(stored["date"]) == 0 or stored["date"][-1] < np.datetime64(day)):
        closes = np.append(closes, price)
    return closes


def chain_metrics_row(snap: ChainSnapshot, day: date, now: datetime) -> dict:
    """metrics_daily values for `day` from a chain + stored closes (today's live price appended if
    today's bar is not stored yet)."""
    q = snap.quote
    b = bars_repo.get_bars(q.symbol, start=day - timedelta(days=HV_LOOKBACK_DAYS), end=day)
    return {
        **chainmetrics.compute_chain_metrics(snap, now),
        **vol.hv_snapshot(closes_with_live(b, day, q.price)),
        "close": q.price, "change_pct": q.change_pct, "volume": q.volume, "delayed": snap.delayed,
    }


def writes_metrics(day: date, now: datetime, final: bool) -> bool:
    """Whether a fetch at `now` may (over)write session `day`'s metrics_daily row: while the session is
    open and until its closing data has arrived (final_since), or for a closing snapshot (`final`).
    Later fetches (evening, weekend, next pre-market) only refresh option_latest/quotes_latest: they
    never overwrite the finalized row, nor create one for a session that has none."""
    return final or now < final_since(day)


def is_stats_only(source: str | None) -> bool:
    """Whether a metrics_daily row holds only pushed underlying stats (no chain-derived values)."""
    return source is not None and source.endswith(STATS_SUFFIX)


def _chain_row(snap: ChainSnapshot, day: date, now: datetime, existing: dict | None) -> dict:
    """The chain-derived row; values the chain cannot give are kept from a stats-only row."""
    row = chain_metrics_row(snap, day, min(now, session_close(day)))
    if existing and is_stats_only(existing.get("source")):
        for k in metrics_repo.metric_columns():
            if row.get(k) is None and existing.get(k) is not None:
                row[k] = existing[k]
    return row


def store_chain(snap: ChainSnapshot, now: datetime, final: bool = False, *, source: str | None = None) -> None:
    """Persist a chain from `source` ('ibkr', 'vendor', ...; None = unknown) observed at `now`:
    - option_latest + quotes_latest, unless a newer chain is stored (then those keep the newer one);
    - the optionable flag;
    - the session's metrics row, per writes_metrics, unless it came from a chain source that outranks
      `source` (time to expiry measured at `now`, or at the close once it passed);
    - `final` (a closing snapshot, `now` after the close): the session's end-of-day archive, unless an
      archive from a source that outranks `source` is stored."""
    symbol = snap.quote.symbol
    stored_at = options_repo.latest_fetched_at(symbol)
    if stored_at is None or snap.fetched_at >= stored_at:
        options_repo.replace_option_latest(snap, source)
        quotes_repo.upsert_quotes([snap.quote], source)
    symbols_repo.set_optionable(symbol, True)
    day = session_date(now)
    if writes_metrics(day, now, final):
        existing = metrics_repo.get_metrics(symbol, day)
        prev = existing.get("source") if existing else None
        if existing is None or is_stats_only(prev) or not options_repo.outranks(prev, source):
            metrics_repo.upsert_metrics(day, symbol, _chain_row(snap, day, now, existing), source)
    if final and now >= session_close(day):
        options_repo.write_eod_chain(day, snap, source)


# pushed underlying-stats key -> metrics_daily column
STATS_FIELDS = {"price": "close", "change_pct": "change_pct", "volume": "volume",
                "call_volume": "call_volume", "put_volume": "put_volume", "call_oi": "call_oi",
                "put_oi": "put_oi", "iv30": "iv30", "hv30": "hv30", "delayed": "delayed"}


def stats_row(stat: Mapping, day: date, stored_bars: dict[str, np.ndarray] | None) -> dict:
    """metrics values from one pushed stats entry: its fields, plus HV windows from stored closes
    (and the pushed price as today's close) where the push has none."""
    pushed = {col: stat[key] for key, col in STATS_FIELDS.items() if stat.get(key) is not None}
    hv: dict = {}
    if stored_bars is not None:
        keep = stored_bars["date"] <= np.datetime64(day)
        hv = vol.hv_snapshot(closes_with_live({k: v[keep] for k, v in stored_bars.items()}, day, stat.get("price")))
    return {**{k: v for k, v in hv.items() if v is not None}, **pushed}


def store_underlying_stats(session: date, stats: Sequence[Mapping], source: str) -> int:
    """Store pushed per-underlying option stats ({symbol, price, change_pct, volume, call_volume,
    put_volume, call_oi, put_oi, iv30, hv30}; missing keys allowed) into session `session`'s
    metrics rows. A row with chain-derived values only gets its NULL fields filled (chain-derived
    values win); otherwise the stats replace the previous stats (row source '<source>:stats'). Each
    iv30 is also recorded in the IV history (vol_history, kind iv30). Returns rows written."""
    syms = [st["symbol"] for st in stats]
    if not syms:
        return 0
    existing = {s: r for s, r in metrics_repo.metrics_on(session).items() if s in set(syms)}
    hist = bars_repo.bars_since(session - timedelta(days=HV_LOOKBACK_DAYS), syms)
    cols = metrics_repo.metric_columns()
    rows: dict[str, tuple[dict, str | None]] = {}
    for st in stats:
        sym = st["symbol"]
        new = stats_row(st, session, hist.get(sym))
        ex = existing.get(sym)
        if ex is not None and not is_stats_only(ex.get("source")):
            vals = {c: ex[c] for c in cols}
            vals.update({k: v for k, v in new.items() if vals.get(k) is None})
            rows[sym] = (vals, ex.get("source"))
        else:
            rows[sym] = (new, f"{source}{STATS_SUFFIX}")
    n = metrics_repo.upsert_metrics_many(session, rows)
    for st in stats:
        if st.get("iv30") is not None and st["iv30"] > 0:
            vol_history_repo.upsert(st["symbol"], "iv30", source, [(session, float(st["iv30"]))])
    return n


def require_source() -> ChainSource:
    src = chain_source.configured()
    if src is None:
        raise RuntimeError("no server-side chain source configured (OPTIONLENS_CHAIN_SOURCE=none): "
                           "chains come from the IBKR bridge")
    return src


async def refresh_chain(symbol: str, final: bool = False) -> ChainSnapshot | None:
    """Fetch one symbol's chain from the configured ChainSource and store it (`final`: closing sweep).
    None (and optionable=false) when the source lists no options. RuntimeError without a source."""
    snap = await require_source().fetch_chain(symbol)
    if not snap.contracts:
        await asyncio.to_thread(quotes_repo.upsert_quotes, [snap.quote], SOURCE_TAG)
        await asyncio.to_thread(symbols_repo.set_optionable, symbol, False)
        return None
    await asyncio.to_thread(lambda: store_chain(snap, now_ny(), final, source=SOURCE_TAG))
    return snap


async def sweep(symbols: list[str] | None = None, final: bool = False) -> str:
    """Refresh chains of the universe (or given symbols) from the configured ChainSource, then fire
    on_sweep_complete. `final`: the closing sweep (archives each chain). RuntimeError without a source."""
    require_source()
    syms = symbols or await asyncio.to_thread(symbols_repo.universe_symbols)
    t0 = _time.monotonic()
    done: list[str] = []

    async def one(sym: str) -> None:
        if await refresh_chain(sym, final) is not None:
            done.append(sym)
        if len(done) % 100 == 0 and done:
            await report_progress(f"{len(done)}/{len(syms)} chains")

    ok, failed = await run_bounded(syms, one, SWEEP_CONCURRENCY, "sweep")
    if failed:  # second pass for transient failures (e.g. a rate-limit burst)
        ok2, failed = await run_bounded(failed, one, SWEEP_CONCURRENCY, "sweep-retry")
        ok += ok2
    detail = (f"{len(done)} chains, {ok - len(done)} not optionable, {len(failed)} failed, "
              f"{_time.monotonic() - t0:.0f}s")
    if failed:
        detail += f" (failed: {','.join(failed[:20])})"
    await hooks.fire(hooks.on_sweep_complete, session_date(now_ny()), sorted(done))
    return detail


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


def archive_unarchived(day: date, symbols: list[str] | None = None) -> int:
    """Archive stored chains fetched after the session's closing data arrived whose (day, symbol) has
    no archive yet (closing snapshots were archived when stored). Returns rows written."""
    have = options_repo.eod_sources([day], symbols)[day]
    todo = sorted(options_repo.fetched_since(final_since(day), symbols) - set(have))
    return options_repo.write_option_eod(day, final_since(day), todo) if todo else 0


async def eod(symbols: list[str] | None = None) -> str:
    """Close a session: the closing vendor sweep (only with a ChainSource), then option_eod for chains
    stored after the closing data arrived and not archived yet, daily_bars from their quotes, on_eod
    hooks (report snapshots, digest). Without `symbols`: every stored chain (pushed or fetched)."""
    day = eod_day()
    sweep_detail = ""
    if chain_source.configured() is not None:
        sweep_detail = f"; sweep: {await sweep(symbols, final=True)}"
    n_eod = await asyncio.to_thread(archive_unarchived, day, symbols)
    bars = await asyncio.to_thread(store_quote_bars, day, symbols)
    await hooks.fire(hooks.on_eod, day, sorted({b.symbol for b in bars}))
    if not n_eod and not bars:
        raise NothingArchived(f"{day}: nothing archived — no closing chain was stored after the close (is IB Gateway "
                              f"connected with the bridge on between the close and 20:00 ET?){sweep_detail}")
    return f"{day}: {n_eod:,} option_eod rows, {len(bars):,} bars{sweep_detail}"


class NothingArchived(RuntimeError):
    """A trading day's EOD found no closing data to archive (recorded as a failed run, not as OK)."""
