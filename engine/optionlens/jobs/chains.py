"""Option-chain pipeline: storing chains from any source (IBKR ingest, server-side ChainSources such as
Questrade), the server-side sweeps (intraday, the closing snapshot at close + 20 min and after-hours fills of an
uncovered last session) and the end-of-day archive.

Precedence for one session's metrics_daily row (docs/INTEGRATION.md §6 "After-hours fills"):
- the session's own data (fetched while it is open and until its closing data arrived, or its closing snapshot)
  always replaces an after-hours fill, and among itself follows jobs/precedence.py (closing snapshot, then
  real-time, then quoted contracts, then the configured source order; also for option_eod);
- an after-hours fill (fetched later: evening, weekend, next pre-market) only creates a row for a session that has
  none, or refreshes an earlier fill (same precedence); it never touches a row of the session's own data;
- option_latest and quotes_latest always hold the newest data with its source; option_eod is written only from a
  closing snapshot (`final`).
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime, timedelta
from typing import Mapping, Sequence

import numpy as np

from ..analytics import chainmetrics, vol
from ..analytics.calendar import NY, OPEN, is_market_open, last_completed_session, now_ny
from ..models import Bar, ChainSnapshot
from ..repo import bars as bars_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..repo import vol_history as vol_history_repo
from ..sources import chain_source
from ..sources.chain_source import ChainSource, NotListed
from . import bridge_plan, hooks, precedence
from ._util import after_session, eod_deadline, final_since, report_progress, run_bounded, session_close, session_date

log = logging.getLogger(__name__)

HV_LOOKBACK_DAYS = 400  # calendar days of closes loaded for HV252
SWEEP_CONCURRENCY = 4
FILL_RETRY_S = 6 * 3600   # an after-hours fill asks a symbol the source sent nothing for again after this
STATS_SUFFIX = ":stats"  # metrics_daily.source of a row holding only pushed underlying stats
AFTER_HOURS = "after_hours"  # metrics_daily column: the row is an after-hours fill (schema.sql)


def closes_with_live(stored: dict[str, np.ndarray], day: date, price: float | None) -> np.ndarray:
    """Stored closes through `day`, with today's live price appended if `day`'s bar is not stored yet."""
    closes = stored["close"]
    if price and (len(stored["date"]) == 0 or stored["date"][-1] < np.datetime64(day)):
        closes = np.append(closes, price)
    return closes


def session_bar(bars: dict[str, np.ndarray], day: date) -> dict:
    """close, change_pct (vs the previous stored bar) and volume of session `day` from stored daily bars; {} when
    its bar is not stored. An after-hours fill takes the session's close from here: IBKR's frozen last price of a
    stock can be an extended-hours trade."""
    dates = bars["date"]
    i = int(np.searchsorted(dates, np.datetime64(day)))
    if i >= len(dates) or dates[i] != np.datetime64(day) or not bars["close"][i] > 0:
        return {}
    close = float(bars["close"][i])
    prev = float(bars["close"][i - 1]) if i > 0 and bars["close"][i - 1] > 0 else None
    vol_ = bars["volume"][i]
    return {"close": close, **({"change_pct": close / prev - 1.0} if prev else {}),
            **({"volume": int(vol_)} if np.isfinite(vol_) else {})}


def chain_metrics_row(snap: ChainSnapshot, day: date, now: datetime, after_hours: bool = False) -> dict:
    """metrics_daily values for `day` from a chain + stored closes (today's live price appended if
    today's bar is not stored yet); an after-hours fill takes close / change / volume from the session's
    stored daily bar when there is one."""
    q = snap.quote
    b = bars_repo.get_bars(q.symbol, start=day - timedelta(days=HV_LOOKBACK_DAYS), end=day)
    return {
        **chainmetrics.compute_chain_metrics(snap, now),
        **vol.hv_snapshot(closes_with_live(b, day, q.price)),
        "close": q.price, "change_pct": q.change_pct, "volume": q.volume, "delayed": snap.delayed,
        **(session_bar(b, day) if after_hours else {}), AFTER_HOURS: after_hours,
    }


def writes_metrics(day: date, now: datetime, final: bool) -> bool:
    """Whether a chain fetched at `now` is session `day`'s own data for its metrics_daily row: fetched while
    the session is open and until its closing data has arrived (final_since), or a closing snapshot
    (`final`). A later fetch (evening, weekend, next pre-market) is an after-hours fill (see module doc)."""
    return final or now < final_since(day)


def is_stats_only(source: str | None) -> bool:
    """Whether a metrics_daily row holds only pushed underlying stats (no chain-derived values)."""
    return source is not None and source.endswith(STATS_SUFFIX)


def is_after_hours(row: Mapping | None) -> bool:
    """Whether a stored metrics_daily row is an after-hours fill."""
    return bool(row and row.get(AFTER_HOURS))


def may_touch(existing: Mapping | None, after_hours: bool) -> bool:
    """The one tier rule: an after-hours push never touches a row of the session's own data (kind-specific
    rules decide the rest)."""
    return existing is None or not after_hours or is_after_hours(existing)


def _chain_replaces(existing: Mapping | None, new: precedence.Quality, after_hours: bool) -> bool:
    """Whether a chain-derived row replaces the stored row (after may_touch): none stored, the session's own data
    over a fill, or within one tier a stats-only row or a chain that wins jobs/precedence.py."""
    if existing is None or is_after_hours(existing) != after_hours or is_stats_only(existing.get("source")):
        return True
    return precedence.replaces(precedence.of_metrics_row(existing), new)


def _chain_row(snap: ChainSnapshot, day: date, now: datetime, existing: dict | None, after_hours: bool,
               quality: precedence.Quality) -> dict:
    """The chain-derived row (with its precedence quality); values the chain cannot give are kept from a
    stats-only row of the same tier."""
    row = {**chain_metrics_row(snap, day, min(now, session_close(day)), after_hours),
           "chain_final": quality.final, "chain_quoted": quality.quoted}
    if existing and is_stats_only(existing.get("source")) and is_after_hours(existing) == after_hours:
        for k in metrics_repo.metric_columns():
            if row.get(k) is None and existing.get(k) is not None:
                row[k] = existing[k]
    return row


def store_chain(snap: ChainSnapshot, now: datetime, final: bool = False, *, source: str | None = None) -> None:
    """Persist a chain from `source` ('ibkr', 'questrade', ...; None = unknown) observed at `now`:
    - option_latest + quotes_latest, unless a newer chain is stored (then those keep the newer one);
    - the optionable flag;
    - the session's metrics row per the module doc's precedence (writes_metrics: the session's own data, else
      an after-hours fill; time to expiry measured at `now`, or at the close once it passed);
    - `final` (a closing snapshot, `now` after the close): the session's end-of-day archive, unless the stored
      archive wins jobs/precedence.py."""
    symbol = snap.quote.symbol
    stored_at = options_repo.latest_fetched_at(symbol)
    if stored_at is None or snap.fetched_at >= stored_at:
        options_repo.replace_option_latest(snap, source)
        quotes_repo.upsert_quotes([snap.quote], source)
    symbols_repo.set_optionable(symbol, True)
    day = session_date(now)
    after_hours = not writes_metrics(day, now, final)
    quality = precedence.of_snapshot(snap, source, final)
    existing = metrics_repo.get_metrics(symbol, day)
    if may_touch(existing, after_hours) and _chain_replaces(existing, quality, after_hours):
        metrics_repo.upsert_metrics(day, symbol, _chain_row(snap, day, now, existing, after_hours, quality), source)
    if final and now >= session_close(day):
        stored = options_repo.eod_quality(day, [symbol]).get(symbol)
        if precedence.replaces(None if stored is None else precedence.of_archive(stored), quality):
            options_repo.write_eod_chain(day, snap, source)


# pushed underlying-stats key -> metrics_daily column
STATS_FIELDS = {"price": "close", "change_pct": "change_pct", "volume": "volume",
                "call_volume": "call_volume", "put_volume": "put_volume", "call_oi": "call_oi",
                "put_oi": "put_oi", "iv30": "iv30", "hv30": "hv30", "delayed": "delayed"}


def stats_row(stat: Mapping, day: date, stored_bars: dict[str, np.ndarray] | None, after_hours: bool = False) -> dict:
    """metrics values from one pushed stats entry: its fields, plus HV windows from stored closes
    (and the pushed price as today's close) where the push has none; an after-hours fill takes close /
    change / volume from the session's stored daily bar when there is one."""
    pushed = {col: stat[key] for key, col in STATS_FIELDS.items() if stat.get(key) is not None}
    hv: dict = {}
    bar: dict = {}
    if stored_bars is not None:
        keep = stored_bars["date"] <= np.datetime64(day)
        upto = {k: v[keep] for k, v in stored_bars.items()}
        hv = vol.hv_snapshot(closes_with_live(upto, day, stat.get("price")))
        bar = session_bar(upto, day) if after_hours else {}
    return {**{k: v for k, v in hv.items() if v is not None}, **pushed, **bar}


def store_underlying_stats(session: date, stats: Sequence[Mapping], source: str, after_hours: bool = False) -> int:
    """Store pushed per-underlying option stats ({symbol, price, change_pct, volume, call_volume,
    put_volume, call_oi, put_oi, iv30, hv30}; missing keys allowed) into session `session`'s
    metrics rows, per the module doc's precedence (`after_hours`: an after-hours fill, which never touches a
    row of the session's own data). A row with chain-derived values only gets its NULL fields filled
    (chain-derived values win); otherwise the stats replace the previous stats (row source
    '<source>:stats'). The iv30 of each row written is also recorded in the IV history (vol_history, kind
    iv30). Unknown values stay NULL (never 0). Returns rows written."""
    syms = [st["symbol"] for st in stats]
    if not syms:
        return 0
    existing = {s: r for s, r in metrics_repo.metrics_on(session).items() if s in set(syms)}
    hist = bars_repo.bars_since(session - timedelta(days=HV_LOOKBACK_DAYS), syms)
    cols = metrics_repo.metric_columns()
    rows: dict[str, tuple[dict, str | None]] = {}
    for st in stats:
        sym = st["symbol"]
        ex = existing.get(sym)
        if not may_touch(ex, after_hours):
            continue
        new = stats_row(st, session, hist.get(sym), after_hours)
        if ex is not None and not is_stats_only(ex.get("source")):
            vals = {c: ex[c] for c in cols}
            vals.update({k: v for k, v in new.items() if vals.get(k) is None})
            rows[sym] = (vals, ex.get("source"))
        else:
            rows[sym] = ({**new, AFTER_HOURS: after_hours}, f"{source}{STATS_SUFFIX}")
    n = metrics_repo.upsert_metrics_many(session, rows) if rows else 0
    for st in stats:   # tagged '<source>:stats': one day's value, not a backfilled history (ingest.wants)
        if st["symbol"] in rows and st.get("iv30") is not None and st["iv30"] > 0:
            vol_history_repo.upsert(st["symbol"], "iv30", f"{source}{STATS_SUFFIX}", [(session, float(st["iv30"]))])
    return n


def server_sources() -> list[ChainSource]:
    """The configured server-side ChainSources; RuntimeError when there is none."""
    srcs = chain_source.server_sources()
    if not srcs:
        raise RuntimeError("no server-side chain source configured (OPTIONLENS_CHAIN_SOURCES): "
                           "chains come from the IBKR bridge")
    return srcs


def _prepare(snap: ChainSnapshot, source: str) -> ChainSnapshot:
    for fn in hooks.chain_preparers:
        snap = fn(snap, source)
    return snap


async def refresh_chain(src: ChainSource, symbol: str, final: bool = False) -> ChainSnapshot | None:
    """Fetch one symbol's chain from `src`, complete it (hooks.chain_preparers: IV / greeks) and store it
    (`final`: closing snapshot). None (and optionable=false) when the source lists no options. Raises NotListed
    when the source does not carry the symbol, net.FetchError on failure."""
    snap = await src.fetch_chain(symbol, final)
    if not snap.contracts:
        await asyncio.to_thread(quotes_repo.upsert_quotes, [snap.quote], src.name)
        await asyncio.to_thread(symbols_repo.set_optionable, symbol, False)
        return None
    now = now_ny()

    def store() -> None:
        store_chain(_prepare(snap, src.name), now, final, source=src.name)
    await asyncio.to_thread(store)
    return snap


async def _sweep_source(src: ChainSource, syms: list[str], final: bool) -> tuple[list[str], str]:
    """Sweep `syms` from one source. Returns (symbols stored, detail)."""
    t0 = _time.monotonic()
    done: list[str] = []
    skipped: list[str] = []

    async def one(sym: str) -> None:
        if src.unavailable() is not None:   # e.g. the token was refused mid-sweep: stop asking
            skipped.append(sym)
            return
        try:
            if await refresh_chain(src, sym, final) is not None:
                done.append(sym)
        except NotListed:
            skipped.append(sym)
            return
        if len(done) % 100 == 0 and done:
            await report_progress(f"{src.name}: {len(done)}/{len(syms)} chains")

    ok, failed = await run_bounded(syms, one, SWEEP_CONCURRENCY, f"sweep {src.name}")
    if failed:  # second pass for transient failures (e.g. a rate-limit burst)
        ok2, failed = await run_bounded(failed, one, SWEEP_CONCURRENCY, f"sweep-retry {src.name}")
        ok += ok2
    detail = (f"{src.name}: {len(done)} chains, {ok - len(done) - len(skipped)} not optionable, "
              f"{len(skipped)} not listed/skipped, {len(failed)} failed, {_time.monotonic() - t0:.0f}s")
    if failed:
        detail += f" (failed: {','.join(failed[:20])})"
    return done, detail


async def sweep(symbols: list[str] | None = None, final: bool = False) -> str:
    """Refresh chains of the universe + the cash indices (bridge_plan.EOD_INDICES; or the given symbols) from every available server-side
    ChainSource, then fire on_sweep_complete. `final`: the closing snapshot (archived per jobs/precedence.py).
    A configured source that is not available (e.g. Questrade not connected) is reported, not swept.
    RuntimeError without any server-side source."""
    srcs = server_sources()
    syms = symbols or list(dict.fromkeys([*await asyncio.to_thread(symbols_repo.universe_symbols), *bridge_plan.EOD_INDICES]))
    done: set[str] = set()
    details = []
    for src in srcs:
        why = src.unavailable()
        if why is not None:
            details.append(f"{src.name}: {why}")
            continue
        got, detail = await _sweep_source(src, syms, final)
        done.update(got)
        details.append(detail)
    if done:
        await hooks.fire(hooks.on_sweep_complete, session_date(now_ny()), sorted(done))
    return "; ".join(details)


async def sweep_final() -> str:
    """The closing snapshot from the server-side sources: from close + 20 min (the closing data has arrived) until
    the closing-chain deadline (20:00 ET, as for the bridge); later the after-hours fill covers the session."""
    now = now_ny()
    day = session_date(now)
    if not final_since(day) <= now < eod_deadline(day):
        return f"skipped: closing snapshots of {day} are taken from {final_since(day):%H:%M} to {eod_deadline(day):%H:%M} ET"
    return await sweep(final=True)


_fill_attempts: dict[tuple[str, str], float] = {}   # (source, symbol) -> monotonic time of the last fill attempt


def fill_symbols(now: datetime, syms: list[str], source: str) -> list[str]:
    """Of `syms`, those without a chain fetched since the open of the current session (session_date) and not
    attempted by `source` within FILL_RETRY_S (docs/INTEGRATION.md §6 "After-hours fills")."""
    have = options_repo.fetched_since(datetime.combine(session_date(now), OPEN, NY), syms)
    t = _time.monotonic()
    return [s for s in syms if s not in have
            and t - _fill_attempts.get((source, s), -FILL_RETRY_S) >= FILL_RETRY_S]


async def sweep_fill() -> str:
    """After-hours fill: outside the session (evening, weekend, before the open) the universe symbols whose
    current session has no chain yet are fetched once from each available server-side source (watchlists,
    recently opened and most liquid first). A fill never writes the archive and never replaces the session's
    own data (store_chain). Also keeps idle tokens alive (source keepalive)."""
    now = now_ny()
    if not after_session(now):
        return "skipped: the session is open or its closing data has not arrived yet"
    srcs = server_sources()
    universe = await asyncio.to_thread(symbols_repo.universe_symbols)
    order = await asyncio.to_thread(bridge_plan.prioritized, list(dict.fromkeys([*universe, *bridge_plan.EOD_INDICES])), now)
    done: set[str] = set()
    details = []
    for src in srcs:
        await src.keepalive()
        why = src.unavailable()
        if why is not None:
            details.append(f"{src.name}: {why}")
            continue
        todo = await asyncio.to_thread(fill_symbols, now, order, src.name)
        if not todo:
            details.append(f"{src.name}: session {session_date(now)} covered")
            continue
        t = _time.monotonic()
        _fill_attempts.update({(src.name, s): t for s in todo})
        got, detail = await _sweep_source(src, todo, final=False)
        done.update(got)
        details.append(f"fill of {session_date(now)}: {detail}")
    if done:
        await hooks.fire(hooks.on_sweep_complete, session_date(now), sorted(done))
    return "; ".join(details)


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
    """Close a session: option_eod for chains stored after the closing data arrived and not archived yet (closing
    snapshots were archived when stored), daily_bars from their quotes, on_eod hooks (report snapshots, digest).
    Without `symbols`: every stored chain (pushed or fetched)."""
    day = eod_day()
    n_eod = await asyncio.to_thread(archive_unarchived, day, symbols)
    bars = await asyncio.to_thread(store_quote_bars, day, symbols)
    await hooks.fire(hooks.on_eod, day, sorted({b.symbol for b in bars}))
    if not n_eod and not bars:
        raise NothingArchived(f"{day}: nothing archived — no closing chain was stored after the close (is IB Gateway "
                              f"connected with the bridge on between the close and 20:00 ET, or Questrade connected?)")
    return f"{day}: {n_eod:,} option_eod rows, {len(bars):,} bars"


class NothingArchived(RuntimeError):
    """A trading day's EOD found no closing data to archive (recorded as a failed run, not as OK)."""
