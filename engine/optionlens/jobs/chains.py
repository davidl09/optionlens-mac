"""Option-chain pipeline: storing chains from any source (IBKR ingest, server-side ChainSources such as
Questrade), the server-side sweeps (intraday, the closing snapshot at close + 20 min and after-hours fills of an
uncovered last session) and the end-of-day archive.

Precedence for one session's metrics_daily row (docs/INTEGRATION.md §6 "After-hours fills"):
- the session's own data (fetched while it is open and until its closing data arrived, or its closing snapshot)
  always replaces an after-hours fill, and among itself follows jobs/precedence.py (closing snapshot, then
  real-time, then quoted contracts, then the fresher fetch; also for option_eod);
- an after-hours fill (fetched later: evening, weekend, next pre-market) only creates a row for a session that has
  none, or refreshes an earlier fill (same precedence); it never touches a row of the session's own data;
- option_latest (the served chain) follows jobs/precedence.py too, across sessions (a later session's chain always
  replaces it); quotes_latest holds the newest quote; option_eod is written only from a closing snapshot (`final`).
Every stored row keeps its `source`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time as _time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Mapping, Sequence

import numpy as np

from ..analytics import chainmetrics, computed_iv, markets, vol
from ..analytics.calendar import NY, NYSE, OPEN, TradingCalendar, now_ny
from ..models import Bar, ChainSnapshot, Quote
from ..net import FetchError
from ..repo import bars as bars_repo
from ..repo import halts as halts_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..repo import vol_history as vol_history_repo
from ..repo._sql import utc_naive
from ..sources import chain_source
from ..sources.chain_source import ChainSource, NotListed
from . import bridge_plan, hooks, precedence, source_health
from ._util import after_session, eod_deadline, final_since, report_progress, run_bounded, session_close, session_date

log = logging.getLogger(__name__)

HV_LOOKBACK_DAYS = 400  # calendar days of closes loaded for HV252
SWEEP_CONCURRENCY = 4
CONFIRMED_RETRY_S = 6 * 3600   # after-hours fill: a symbol the source has no chain for (not listed / no options)
FAILED_RETRY_S = 30 * 60       # ... and one whose fetch failed, asked again after this (never counted as coverage)
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


def writes_metrics(day: date, now: datetime, final: bool, cal: TradingCalendar = NYSE) -> bool:
    """Whether a chain fetched at `now` is session `day`'s own data for its metrics_daily row: fetched while
    the session is open and until its closing data has arrived (final_since), or a closing snapshot
    (`final`). A later fetch (evening, weekend, next pre-market) is an after-hours fill (see module doc).
    `cal`: the underlying's exchange calendar."""
    return final or now < final_since(day, cal)


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
    cal = markets.calendar_for(snap.quote.symbol)
    row = {**chain_metrics_row(snap, day, min(now, session_close(day, cal)), after_hours),
           "chain_final": quality.final, "chain_quoted": quality.quoted,
           "chain_fetched_at": utc_naive(quality.fetched_at) if quality.fetched_at else None}
    if existing and is_stats_only(existing.get("source")) and is_after_hours(existing) == after_hours:
        for k in metrics_repo.metric_columns():
            if row.get(k) is None and existing.get(k) is not None:
                row[k] = existing[k]
    return row


def store_chain(snap: ChainSnapshot, now: datetime, final: bool = False, *, source: str | None = None) -> bool:
    """Persist a chain from `source` ('ibkr', 'questrade', ...; None = unknown) observed at `now`, each part per
    jobs/precedence.py:
    - the served chain (option_latest), when the chain wins against the served one; when it loses, the served
      chain takes this chain's source IVs where its own are computed (adopt_source_iv: any source's IV before a
      computed one);
    - the underlying's quote (quotes_latest) when it is the newest one;
    - the optionable flag;
    - the session's metrics row per the module doc's precedence (writes_metrics: the session's own data, else
      an after-hours fill; time to expiry measured at `now`, or at the close once it passed);
    - `final` (a closing snapshot, `now` after the close): the session's end-of-day archive.
    Returns whether the chain is now the served one."""
    symbol = snap.quote.symbol
    quality = precedence.of_snapshot(snap, source, final)
    served = precedence.of_latest(options_repo.latest_quality(symbol), markets.calendar_for(symbol))
    wins = precedence.replaces(served, quality)
    if wins:
        options_repo.replace_option_latest(snap, source, final)
    if wins or served is None or served.fetched_at is None or snap.fetched_at >= served.fetched_at:
        quotes_repo.upsert_quotes([snap.quote], source)   # the stock quote: the newest one
    record_halt(snap.quote, source)
    symbols_repo.set_optionable(symbol, True)
    _store_session(snap, now, final, source, quality)
    if not wins and served is not None and served.session == quality.session and served.source != source:
        adopt_source_iv(symbol, served, snap)
    return wins


def _store_session(snap: ChainSnapshot, now: datetime, final: bool, source: str | None,
                   quality: precedence.Quality) -> None:
    """The session's metrics row and (closing snapshots) archive from `snap`, where it wins (store_chain)."""
    symbol = snap.quote.symbol
    cal = markets.calendar_for(symbol)
    day = session_date(now, cal)
    after_hours = not writes_metrics(day, now, final, cal)
    existing = metrics_repo.get_metrics(symbol, day)
    if may_touch(existing, after_hours) and _chain_replaces(existing, quality, after_hours):
        metrics_repo.upsert_metrics(day, symbol, _chain_row(snap, day, now, existing, after_hours, quality), source)
    if final and now >= session_close(day, cal):
        stored = options_repo.eod_quality(day, [symbol]).get(symbol)
        if precedence.replaces(None if stored is None else precedence.of_archive(stored), quality):
            options_repo.write_eod_chain(day, snap, source)


def record_halt(quote: Quote, source: str | None) -> list[str]:
    """The quote's halt flag (when its source reports one) -> halt_status. Returns the symbols whose state changed."""
    if quote.halted is None or source is None:
        return []
    return halts_repo.record([(quote.symbol, quote.halted, quote.ts)], source)


def adopt_source_iv(symbol: str, served: precedence.Quality, donor: ChainSnapshot) -> int:
    """The served chain takes `donor`'s (a losing chain of the same session) source-given IV and greeks for its
    contracts whose IV is engine-computed or missing (analytics.computed_iv.adopt_source_iv), and its metrics row and
    archive are rebuilt from it where they hold that chain. Returns contracts changed."""
    stored = options_repo.get_chain(symbol)
    if stored is None:
        return 0
    changed = computed_iv.adopt_source_iv(stored.contracts, donor.contracts)
    if not changed:
        return 0
    options_repo.update_source_iv(symbol, changed)
    merged = options_repo.get_chain(symbol)
    if merged is None or served.fetched_at is None:
        return 0
    raw = options_repo.latest_quality(symbol) or {}
    merged = dataclasses.replace(merged, fetched_at=served.fetched_at, delayed=raw.get("delayed"),
                                 contracts_listed_at=options_repo.latest_contracts_listed_at(symbol))
    when = merged.fetched_at.astimezone(NY)
    _store_session(merged, when, served.final, served.source,
                   dataclasses.replace(served, quoted=precedence.quoted(merged.contracts)))
    return len(changed)


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
    cols = metrics_repo.row_columns()
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
    when the source does not carry the symbol, net.FetchError on failure. Every outcome is recorded in
    jobs.source_health (failure reasons, per-symbol coverage)."""
    try:
        snap = await src.fetch_chain(symbol, final)
    except NotListed:
        source_health.record(src.name, symbol, "not_listed", now=now_ny())
        raise
    except Exception as e:
        source_health.record(src.name, symbol, "failed", failure_reason(e), now=now_ny())
        raise
    if not snap.contracts:
        await asyncio.to_thread(quotes_repo.upsert_quotes, [snap.quote], src.name)
        await asyncio.to_thread(record_halt, snap.quote, src.name)
        await asyncio.to_thread(symbols_repo.set_optionable, symbol, False)
        source_health.record(src.name, symbol, "no_options", now=now_ny())
        return None
    now = now_ny()

    def store() -> None:
        store_chain(_prepare(snap, src.name), now, final, source=src.name)
    await asyncio.to_thread(store)
    source_health.record(src.name, symbol, "ok", now=now)
    return snap


def failure_reason(e: BaseException) -> str:
    """The grouping key of a failed fetch: the source's own (e.g. Questrade's "403 code 1016 (missing market-data
    permission)"), else HTTP status / exception type and message."""
    r = getattr(e, "reason", None)
    if r:
        return str(r)
    if isinstance(e, FetchError):
        return f"{e.status or 'no response'} {type(e).__name__}"
    return f"{type(e).__name__}: {e}"[:160]


def retryable(e: BaseException) -> bool:
    """Whether a failed fetch is worth another try in the same sweep: transient (5xx, timeouts) and rate limits.
    An auth / permission problem stops the source (circuit breaker); a refused request fails that symbol."""
    return getattr(e, "kind", "transient") in ("transient", "rate")


async def _sweep_source(src: ChainSource, syms: list[str], final: bool) -> tuple[list[str], str]:
    """Sweep `syms` from one source; a second pass retries transient failures only. When the source becomes
    unavailable mid-sweep (e.g. Questrade answered 403 code 1016: it needs attention) the rest is not asked for.
    Returns (symbols stored, detail with the grouped failure reasons)."""
    t0 = _time.monotonic()
    done: list[str] = []
    no_options: list[str] = []
    not_listed: list[str] = []
    failed: dict[str, BaseException] = {}
    aborted: list[str] = []

    async def one(sym: str) -> None:
        if src.unavailable() is not None:   # circuit breaker (e.g. the token lacks a permission): stop asking
            aborted.append(sym)
            return
        try:
            if await refresh_chain(src, sym, final) is not None:
                done.append(sym)
            else:
                no_options.append(sym)
            failed.pop(sym, None)
        except NotListed:
            not_listed.append(sym)
            return
        except Exception as e:  # noqa: BLE001 - one symbol must not stop the sweep
            failed[sym] = e
            log.debug("sweep %s %s failed: %s", src.name, sym, e)
            return
        if len(done) % 100 == 0 and done:
            await report_progress(f"{src.name}: {len(done)}/{len(syms)} chains")

    await run_bounded(syms, one, SWEEP_CONCURRENCY, f"sweep {src.name}")
    retry = [s for s, e in failed.items() if retryable(e)]
    if retry and src.unavailable() is None:   # second pass for transient failures (e.g. a burst of 5xx)
        await run_bounded(retry, one, SWEEP_CONCURRENCY, f"sweep-retry {src.name}")
    counts = Counter(failure_reason(e) for e in failed.values())
    detail = (f"{src.name}: {len(done)} chains, {len(no_options)} not optionable, {len(not_listed)} not listed, "
              f"{len(failed)} failed, {_time.monotonic() - t0:.0f}s")
    if counts:
        detail += f" (failures: {source_health.format_failures([{'reason': r, 'count': c} for r, c in counts.most_common(3)])})"
    if aborted:
        detail += f"; stopped, {len(aborted)} not asked: {src.unavailable() or 'source unavailable'}"
    return done, detail


def _sweep_universe() -> list[str]:
    return list(dict.fromkeys([*symbols_repo.universe_symbols(), *bridge_plan.EOD_INDICES]))


async def _fire_sweep_complete(now: datetime, done: set[str]) -> None:
    """on_sweep_complete per market: each market's symbols with that market's session."""
    for m, syms in markets.by_market(sorted(done)).items():
        await hooks.fire(hooks.on_sweep_complete, session_date(now, m.calendar), syms)


async def sweep(symbols: list[str] | None = None, final: bool = False) -> str:
    """Refresh chains of the universe + the cash indices (bridge_plan.EOD_INDICES; or the given symbols) from every
    available server-side ChainSource, then fire on_sweep_complete. Without `symbols`, only the markets trading
    today are swept (a TSX holiday leaves the Canadian symbols out, and the other way round). `final`: the closing
    snapshot (archived per jobs/precedence.py). A configured source that is not available (e.g. Questrade not
    connected, or it needs attention) is reported, not swept. RuntimeError without any server-side source."""
    srcs = server_sources()
    now = now_ny()
    if symbols:
        syms = list(symbols)
    else:
        syms = [s for m, group in markets.by_market(await asyncio.to_thread(_sweep_universe)).items()
                if m.calendar.is_trading_day(now.date()) for s in group]
    if not syms:
        return "skipped: no market of the universe trades today"
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
        await _fire_sweep_complete(now, done)
    return "; ".join(details)


# market code -> the session whose closing snapshot was taken (this process): a later close + 20 min run of the same
# day (e.g. TSX at 16:20 after NYSE's 13:00 early close) does not sweep the market that already closed again.
_final_done: dict[str, date] = {}


def closing_markets(now: datetime) -> list[tuple[markets.Market, date]]:
    """(market, session) pairs whose closing-snapshot window is open at `now` (close + 20 min until 20:00 ET) and
    whose closing snapshot this process has not taken yet."""
    out = []
    for m in markets.MARKETS.values():
        day = session_date(now, m.calendar)
        if final_since(day, m.calendar) <= now < eod_deadline(day) and _final_done.get(m.code) != day:
            out.append((m, day))
    return out


async def sweep_final() -> str:
    """The closing snapshot from the server-side sources, per market: from its close + 20 min (the closing data has
    arrived) until the closing-chain deadline (20:00 ET, as for the bridge); later the after-hours fill covers the
    session."""
    now = now_ny()
    due = closing_markets(now)
    if not due:
        day = session_date(now)
        return (f"skipped: no closing snapshot due (closing snapshots of {day} are taken from "
                f"{final_since(day):%H:%M} to {eod_deadline(day):%H:%M} ET)")
    codes = {m.code for m, _ in due}
    syms = [s for s in await asyncio.to_thread(_sweep_universe) if markets.market_of(s).code in codes]
    if not syms:
        return f"skipped: no {'/'.join(sorted(codes))} symbols to snapshot"
    out = await sweep(syms, final=True)
    for m, day in due:
        _final_done[m.code] = day
    return out


def fill_symbols(now: datetime, syms: list[str], source: str) -> tuple[list[str], int]:
    """(Of `syms`, those an after-hours fill from `source` should fetch, how many wait for a retry after failing).
    A symbol is covered by a chain stored since the open of its market's current session (session_date), or by a
    recent answer that it has none to give (not listed / no options, within CONFIRMED_RETRY_S). A failed attempt is
    never coverage: the symbol is asked again FAILED_RETRY_S later (docs/INTEGRATION.md §6 "After-hours fills")."""
    have: set[str] = set()
    for m, group in markets.by_market(syms).items():
        have |= options_repo.fetched_since(datetime.combine(session_date(now, m.calendar), OPEN, NY), group)
    todo, waiting = [], 0
    for s in syms:
        if s in have:
            continue
        a = source_health.last(source, s)
        age = None if a is None else (now - a.at).total_seconds()
        if a is not None and a.result != "failed" and age < CONFIRMED_RETRY_S:
            continue
        if a is not None and a.result == "failed" and age < FAILED_RETRY_S:
            waiting += 1
            continue
        todo.append(s)
    return todo, waiting


async def sweep_fill() -> str:
    """After-hours fill: outside its market's session (evening, weekend, holiday, before the open) a universe symbol
    whose current session has no chain yet is fetched once from each available server-side source (watchlists,
    recently opened and most liquid first). A fill never writes the archive and never replaces the session's own
    data (store_chain). Also keeps idle tokens alive (source keepalive)."""
    now = now_ny()
    closed = [m for m in markets.MARKETS.values() if after_session(now, m.calendar)]
    if not closed:
        return "skipped: the session is open or its closing data has not arrived yet"
    srcs = server_sources()
    universe = await asyncio.to_thread(_sweep_universe)
    universe = [s for s in universe if markets.market_of(s) in closed]
    order = await asyncio.to_thread(bridge_plan.prioritized, universe, now)
    present = list(markets.by_market(universe))
    label = session_date(now) if present in ([], [markets.US]) else \
        ", ".join(f"{m.code} {session_date(now, m.calendar)}" for m in present)
    done: set[str] = set()
    details = []
    for src in srcs:
        await src.keepalive()
        why = src.unavailable()
        if why is not None:
            details.append(f"{src.name}: {why}")
            continue
        todo, waiting = await asyncio.to_thread(fill_symbols, now, order, src.name)
        if not todo:
            details.append(f"{src.name}: session {label} covered" if not waiting else
                           f"{src.name}: {waiting} symbols of session {label} failed recently; "
                           f"asked again {FAILED_RETRY_S // 60} min after their attempt")
            continue
        got, detail = await _sweep_source(src, todo, final=False)
        done.update(got)
        details.append(f"fill of {label}: {detail}")
    if done:
        await _fire_sweep_complete(now, done)
    return "; ".join(details)


def eod_day(now: datetime | None = None, cal: TradingCalendar = NYSE) -> date:
    """Session the EOD archive is for. ValueError while a session is open or before its closing data
    has arrived (final_since): the archive would hold intraday data."""
    now = now or now_ny()
    if cal.is_market_open(now):
        raise ValueError("market is open: EOD can only run after the close")
    day = cal.last_completed_session(now)
    if now < final_since(day, cal):
        raise ValueError(f"EOD for {day} can only run from {final_since(day, cal):%H:%M} (delayed closing data)")
    return day


def store_quote_bars(day: date, symbols: list[str] | None, cal: TradingCalendar = NYSE) -> list[Bar]:
    """daily_bars for `day` from stored quotes of chains fetched at/after final_since(day) (the closing
    snapshot; quotes without a chain carry no fetch time and are skipped). Universe symbols keep any
    stored bar (Nasdaq / Questrade backfill and refresh own their history); other symbols' bars are replaced, so an
    earlier non-final bar is corrected. Returns the bars offered."""
    final = options_repo.fetched_since(final_since(day, cal), symbols)
    universe = set(symbols_repo.universe_symbols(include_unoptionable=True))
    out = [Bar(sym, day, q.open, q.high, q.low, q.price, q.volume or None)
           for sym, q in quotes_repo.all_quotes().items() if sym in final and q.price]
    bars_repo.upsert_bars([b for b in out if b.symbol in universe], replace=False)
    bars_repo.upsert_bars([b for b in out if b.symbol not in universe])
    return out


def archive_unarchived(day: date, symbols: list[str] | None = None, cal: TradingCalendar = NYSE) -> int:
    """Archive stored chains fetched after the session's closing data arrived whose (day, symbol) has
    no archive yet (closing snapshots were archived when stored). Returns rows written."""
    have = options_repo.eod_sources([day], symbols)[day]
    todo = sorted(options_repo.fetched_since(final_since(day, cal), symbols) - set(have))
    return options_repo.write_option_eod(day, final_since(day, cal), todo) if todo else 0


def _closing_symbols(m: markets.Market, day: date, symbols: list[str] | None) -> list[str]:
    """Market m's symbols (of `symbols`, or of every stored chain) with a chain fetched after `day`'s closing data
    arrived: what its EOD archives."""
    return sorted(s for s in options_repo.fetched_since(final_since(day, m.calendar), symbols)
                  if markets.market_of(s) is m)


async def eod(symbols: list[str] | None = None) -> str:
    """Close the latest session, per market: option_eod for chains stored after the closing data arrived and not
    archived yet (closing snapshots were archived when stored), daily_bars from their quotes, on_eod hooks (report
    snapshots, digest: the US market's). Without `symbols`: every stored chain (pushed or fetched). A market closes
    when its latest completed session is the latest one among the markets (on a TSX holiday only the US market
    closes, on a US-only holiday only Canada); NothingArchived when no market had anything to archive."""
    now = now_ny()
    latest = max(m.calendar.last_completed_session(now) for m in markets.MARKETS.values())
    parts, n_total = [], 0
    for m in markets.MARKETS.values():
        cal = m.calendar
        if cal.last_completed_session(now) != latest:
            continue
        day = eod_day(now, cal)
        scoped = await asyncio.to_thread(_closing_symbols, m, day, symbols)
        n_eod = await asyncio.to_thread(archive_unarchived, day, scoped, cal) if scoped else 0
        bars = await asyncio.to_thread(store_quote_bars, day, scoped, cal) if scoped else []
        if m is markets.US:
            await hooks.fire(hooks.on_eod, day, sorted({b.symbol for b in bars}))
        elif not scoped:
            continue
        n_total += n_eod + len(bars)
        parts.append(f"{day}{'' if m is markets.US else f' ({m.code})'}: {n_eod:,} option_eod rows, {len(bars):,} bars")
    if not n_total:
        raise NothingArchived(f"{latest}: nothing archived — no closing chain was stored after the close (is IB Gateway "
                              f"connected with the bridge on between the close and 20:00 ET, or Questrade connected?)")
    return "; ".join(parts)


class NothingArchived(RuntimeError):
    """A trading day's EOD found no closing data to archive (recorded as a failed run, not as OK)."""
