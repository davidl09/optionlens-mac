"""Shared market context for the services layer: dates, on-demand refresh, risk-free rate, dividend
yields, option chains as ChainArrays (one symbol or the whole universe), IV rank, next earnings /
ex-dividend maps, and the option-backtest PriceProvider over stored EOD data.

Everything here is synchronous and may block on the database. Functions that can trigger an
on-demand fetch (`refresh`, `ensure_known`, `load_chain_arrays(ensure_fresh=True)`) must run in a
worker thread (anyio.to_thread / a FastAPI sync path) or in a plain script without a running loop.
Per-sweep caches are cleared by `hooks.on_sweep_complete`.
"""
from __future__ import annotations

import asyncio
import bisect
import functools
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

import anyio.from_thread
import numpy as np

from ..analytics import earnings as earn_a
from ..analytics import stats as stats_a
from ..analytics import markets, vol
from ..analytics.calendar import now_ny
from ..analytics.chain import ChainArrays
from ..analytics.stats import bar_arrays_from_columns
from ..analytics.strategies import close_dt
from ..analytics.dividends import (PARITY_BAND, PARITY_DTE, TRAILING_COVERAGE_DAYS, covers_trailing_year,
                                   implied_yield_from_chain, trailing_yield)
from ..config import get_settings
from ..jobs import hooks, ondemand, prices
from ..jobs._util import after_session, session_close, session_date
from ..jobs.bridge_plan import EOD_INDICES as INDEX_SYMBOLS
from ..models import Bar, Dividend, EarningsEvent, OptionContract, Quote, normalize_symbol
from ..repo import bars as bars_repo
from ..repo import dividends as div_repo
from ..repo import earnings as earn_repo
from ..repo import halts as halts_repo
from ..repo import market as market_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..repo import vol_history as vol_history_repo

INDEX_DEFAULT_Q = {"SPX": 0.013, "XSP": 0.013}   # other indices: 0 (SPEC §6)
PM_ROOTS = frozenset({"SPXW", "NDXP", "RUTW", "VIXW"})  # preferred when an AM root lists the same contract
CHAIN_CACHE_SIZE = 32


# ============================================================================ dates
def today() -> date:
    """Today in New York."""
    return now_ny().date()


def as_date(v: Any) -> date | None:
    """date / datetime / numpy datetime64 / ISO string -> date (None/NaT -> None)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, np.datetime64):
        return None if np.isnat(v) else v.astype("datetime64[D]").astype(object)
    return date.fromisoformat(str(v)[:10])


def as_utc(v: Any) -> datetime | None:
    """numpy datetime64 (naive UTC) / datetime -> aware UTC datetime."""
    if v is None:
        return None
    if isinstance(v, np.datetime64):
        if np.isnat(v):
            return None
        v = v.astype("datetime64[us]").astype(datetime)
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def options_asof(symbol: str) -> datetime | None:
    """Fetch time of the stored chain (aware UTC) or None."""
    return options_repo.latest_fetched_at(normalize_symbol(symbol))


# ============================================================================ on-demand
def run_async(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Run an async job function from sync code: on the server loop when called from an AnyIO
    worker thread, else in a fresh event loop (scripts/tests)."""
    call = functools.partial(fn, *args, **kwargs)
    try:
        anyio.from_thread.run_sync(lambda: None)
    except RuntimeError:
        return asyncio.run(call())
    return anyio.from_thread.run(call)


def ensure_known(symbol: str) -> dict:
    """symbols row for symbol, creating it and backfilling price history on first visit.
    Raises ondemand.UnknownSymbol."""
    sym = normalize_symbol(symbol)
    row = symbols_repo.get_symbol(sym)
    if row is None or prices.lacks_history(bars_repo.coverage([sym]).get(sym)):
        run_async(ondemand.ensure_history, sym)
        row = symbols_repo.get_symbol(sym)
    if row is None:
        raise ondemand.UnknownSymbol(sym)
    return row


REFRESH_MIN_INTERVAL_S = 120.0   # a visit is recorded at most once per symbol per interval; also the bridge's
_attempts: dict[str, float] = {}  # on-demand freshness bound (wants.chains_on_demand_max_age_s)


def refresh(symbol: str) -> dict:
    """ensure_known + record the visit (ondemand.visit), at most once per symbol every REFRESH_MIN_INTERVAL_S.
    Chains are not fetched here: the page starts a refresh round (services.refresh). Returns the symbols row."""
    sym = normalize_symbol(symbol)
    row = symbols_repo.get_symbol(sym)
    last = _attempts.get(sym)
    if row is not None and last is not None and time.monotonic() - last < REFRESH_MIN_INTERVAL_S:
        return row
    run_async(ondemand.visit, sym)
    _attempts[sym] = time.monotonic()
    row = symbols_repo.get_symbol(sym)
    if row is None:
        raise ondemand.UnknownSymbol(sym)
    return row


# ============================================================================ per-sweep memo
_memo: dict[tuple, Any] = {}
_memo_lock = threading.Lock()
_chain_lock = threading.Lock()


def _memoized(name: str, fn: Callable[[], Any]) -> Any:
    key = (name, today())
    with _memo_lock:
        if key in _memo:
            return _memo[key]
    val = fn()
    with _memo_lock:
        _memo[key] = val
    return val


def invalidate(_day: date | None = None, _symbols: list[str] | None = None) -> None:
    """Drop per-sweep caches (registered on hooks.on_sweep_complete)."""
    with _memo_lock:
        _memo.clear()
    with _chain_lock:
        _chains.clear()


hooks.on_sweep_complete.append(invalidate)


def forget_chains(symbols: Iterable[str]) -> None:
    """Drop these symbols' cached ChainArrays (their served rows changed in place: live quotes, live/persist.py)."""
    with _chain_lock:
        for s in symbols:
            _chains.pop(s, None)


# ============================================================================ rates / dividends
def risk_free_rate(day: date | None = None, currency: str = "USD") -> float:
    """The stored T-bill rate of `currency` on/before day (USD: Treasury 13-week, CAD: Bank of Canada 3-month), else
    the configured fallback (RISK_FREE_RATE / RISK_FREE_RATE_CAD)."""
    r = market_repo.rate_on(day, currency)
    return float(r) if r is not None else get_settings().fallback_rate(currency)


def rate_for(symbol: str, day: date | None = None) -> float:
    """risk_free_rate in the currency of `symbol`'s market (a Canadian option is priced with the CAD rate)."""
    return risk_free_rate(day, markets.currency_of(symbol))


def _index_q(symbol: str, kind: str | None) -> float | None:
    return INDEX_DEFAULT_Q.get(symbol, 0.0) if kind == "index" else None


def dividend_yield(symbol: str, spot: float | None, chain_arrays: ChainArrays | None = None,
                   day: date | None = None) -> float:
    """Continuous q: indices by default table; symbols whose stored dividend history covers the
    trailing year: trailing-12m / spot; otherwise (Nasdaq has no history for NYSE/ARCA or Toronto listings; a
    calendar-only row is not a history) implied by put-call parity on the chain (0 when not estimable)."""
    sym = normalize_symbol(symbol)
    row = symbols_repo.get_symbol(sym)
    q = _index_q(sym, row and row["kind"])
    if q is not None:
        return q
    divs = div_repo.for_symbol(sym)
    if covers_trailing_year(divs, day or today()):
        return trailing_yield(divs, spot, day or today())
    ca = chain_arrays if chain_arrays is not None else load_chain_arrays(sym, ensure_fresh=False)
    if ca is None:
        return 0.0
    return implied_yield_from_chain(ca, rate_for(sym, day)) or 0.0


def _implied_yields_all() -> dict[str, float]:
    """Parity-implied q for every stored chain (near-ATM rows only; one query)."""
    t = today()
    near = options_repo.near_atm_by_symbol(t + timedelta(days=PARITY_DTE[0]), t + timedelta(days=PARITY_DTE[1]),
                                           PARITY_BAND)
    out = {}
    for sym, cols in near.items():
        r = rate_for(sym)
        ca = _arrays(sym, cols, r=r, with_src=False)
        q = implied_yield_from_chain(ca, r) if ca is not None else None
        if q is not None:
            out[sym] = q
    return out


def dividend_yield_map(spots: Mapping[str, float]) -> dict[str, float]:
    """Bulk dividend_yield for {symbol: spot} (no per-symbol queries)."""
    def build():
        kinds = {r["symbol"]: r["kind"] for r in symbols_repo.list_symbols()}
        return (kinds, div_repo.symbols_with_history(covering=today() - timedelta(days=TRAILING_COVERAGE_DAYS)),
                div_repo.trailing_sum(today()))
    kinds, with_hist, ttm = _memoized("div_inputs", build)
    implied = None
    out = {}
    for sym, spot in spots.items():
        q = _index_q(sym, kinds.get(sym))
        if q is None and sym in with_hist:
            q = ttm.get(sym, 0.0) / spot if spot and spot > 0 else 0.0
        if q is None:
            implied = implied if implied is not None else _memoized("implied_q", _implied_yields_all)
            q = implied.get(sym, 0.0)
        out[sym] = q
    return out


def rate_fn(currency: str = "USD") -> Callable[[date], float]:
    """r(d): stored T-bill rate of `currency` on/before d (config fallback) — for backtests (cached per sweep)."""
    def build():
        rows = market_repo.rates_between(date(1900, 1, 1), date(2999, 12, 31), currency)
        return [r["date"] for r in rows], [float(r["rate"]) for r in rows]
    days, rates = _memoized(f"rates:{currency}", build)
    fallback = get_settings().fallback_rate(currency)

    def r(d: date) -> float:
        i = bisect.bisect_right(days, d) - 1
        return rates[i] if i >= 0 else (rates[0] if rates else fallback)
    return r


def rate_fn_for(symbol: str) -> Callable[[date], float]:
    """rate_fn in the currency of `symbol`'s market."""
    return rate_fn(markets.currency_of(symbol))


# ============================================================================ bars
def load_bars(symbol: str, start: date | None = None, live: bool = True) -> list[Bar]:
    """Stored daily bars (ascending) since start; with ``live``, today's session from the stored
    quote is appended when the quote is newer than the last stored bar (delayed real data)."""
    sym = normalize_symbol(symbol)
    c = bars_repo.get_bars(sym, start=start)
    floats = lambda a: [None if x != x else float(x) for x in a.tolist()]  # noqa: E731
    vols = [None if v != v else int(v) for v in c["volume"].tolist()]
    out = [Bar(sym, d, o, h, l, cl, v) for d, o, h, l, cl, v in zip(
        c["date"].astype(object).tolist(), floats(c["open"]), floats(c["high"]), floats(c["low"]),
        c["close"].tolist(), vols)]
    if live:
        q = quotes_repo.get_quote(sym)
        d = quote_session(q) if q else None
        if q and q.price and d and (not out or out[-1].date < d) and (start is None or d >= start):
            out.append(Bar(sym, d, q.open, q.high, q.low, float(q.price), q.volume))
    return out


def nasdaq_gap(symbol: str, what: str) -> str | None:
    """Why `what` (from Nasdaq: earnings dates, dividend history, company data) is not available for `symbol`, or
    None when it is: Nasdaq covers US listings only and no free source is used for Canadian ones (SPEC §0)."""
    if ondemand.covered_by_nasdaq(symbol):
        return None
    return (f"{what} for Canadian listings are not available: OptionLens's source (Nasdaq) covers US listings only, "
            "and no free source for Toronto listings is used.")


# ============================================================================ quote / metrics
def quote_session(q: Quote) -> date | None:
    """The trading session a stored quote belongs to (its live daily bar's date): the session of its
    time stamp, so a quote fetched on a weekend or before the open is the previous session's, never a
    bar dated on a non-trading day."""
    return session_date(q.ts, markets.calendar_for(q.symbol)) if q.ts else None


def optionable(symbol: str) -> bool | None:
    """The symbols row's optionable flag: True once a chain was stored, False when a chain source
    confirmed the symbol lists no options, None (unknown) otherwise or for an unknown symbol."""
    row = symbols_repo.get_symbol(normalize_symbol(symbol))
    return row["optionable"] if row else None


_QUOTE_KEYS = ("symbol", "ts", "price", "change", "change_pct", "open", "high", "low", "prev_close", "volume", "bid",
               "ask", "iv30", "vwap")
HALT_FRESH_S = 90 * 60   # a halt flag observed longer ago than this is unknown (sweeps observe it at least hourly)


def halt_flags(symbols: Sequence[str] | None = None, now: datetime | None = None) -> dict[str, dict]:
    """{symbol: {halted, since, source}} of the halt observations younger than HALT_FRESH_S (`since`: when the state
    last changed, None when no change was seen)."""
    now = now or datetime.now(timezone.utc)
    return {s: {"halted": bool(r["halted"]), "since": r["changed_at"], "source": r["source"]}
            for s, r in halts_repo.current(symbols).items()
            if (now - r["observed_at"]).total_seconds() <= HALT_FRESH_S}
_BAR_FILLED = ("price", "change", "change_pct", "open", "high", "low", "prev_close", "volume")
QUOTE_BARS_DAYS = 10   # calendar days of bars read for a quote (the latest bar and the one before it)


def bars_source(symbol: str) -> str:
    """Who supplies a symbol's daily bars: the IBKR bridge for the cash indices, Questrade for Canadian listings,
    Nasdaq for US stocks and ETFs."""
    sym = normalize_symbol(symbol)
    return "ibkr" if sym in INDEX_SYMBOLS else "questrade" if markets.is_canadian(sym) else "nasdaq"


def quote_dict(symbol: str, bars: Sequence[Bar] | None = None) -> dict | None:
    """The symbol's current quote. A stored quote of the latest session with data is used as it is, the fields it
    lacks (OHLC, volume, previous close) filled from the daily bars; without one (none pushed, or older than the last
    stored bar), the latest daily bar stands in: its close, OHLC, volume and change vs the previous close, `source`
    the bars' source and `ts` that session's close. None when there is neither. `after_hours`: the stored quote was
    collected after its session's closing data arrived (IBKR frozen data; jobs._util.after_session). `bars`: the symbol's live bars
    (load_bars), read when not given."""
    sym = normalize_symbol(symbol)
    q = quotes_repo.get_quote(sym)
    if bars is None:
        bars = load_bars(sym, today() - timedelta(days=QUOTE_BARS_DAYS))
    b = stats_a.bar_arrays(bars)
    from_bars = stats_a.latest_bar_quote(b.open, b.high, b.low, b.close, b.volume)
    qd = quote_session(q) if q is not None and q.price else None
    if qd is not None and (not bars or qd >= bars[-1].date):
        out = {f: getattr(q, f) for f in _QUOTE_KEYS}
        for k in _BAR_FILLED:
            if out[k] is None:
                out[k] = from_bars.get(k)
        halt = halt_flags([sym]).get(sym)
        return {**out, "delayed": bool(q.delayed), "after_hours": after_session(q.ts, markets.calendar_for(sym)),
                "source": quotes_repo.source_of(sym), "halted": halt["halted"] if halt else None}
    if not from_bars or from_bars["price"] is None:
        return None
    return {**dict.fromkeys(_QUOTE_KEYS), **from_bars, "symbol": sym,
            "ts": session_close(bars[-1].date, markets.calendar_for(sym)),
            "delayed": False, "after_hours": False, "source": bars_source(sym), "halted": None}


def iv_history(symbol: str, cols: Sequence[str]) -> dict[str, np.ndarray]:
    """The one stored-IV history rule (docs/INTEGRATION.md §6): the symbol's metrics_daily columns
    ({date, <cols>} ascending) whose iv30 is the server's collected value where present, else an
    ingested (IBKR) IV30 of that date. IV rank, vol history charts and the IV proxy read this."""
    return vol_history_repo.history_merged(normalize_symbol(symbol), cols)


def iv_session() -> date | None:
    """Latest date of the IV history (collected or ingested): IV rank is current only on it."""
    return vol_history_repo.latest_iv30_date()


def metrics_series(symbol: str, cols: list[str]) -> dict[str, dict[date, float | None]]:
    """{col: {date: value}} of the symbol's stored metrics, iv30 per iv_history (NaN -> None)."""
    h = iv_history(symbol, cols)
    dates = [as_date(d) for d in h["date"]]
    return {c: {d: (None if v != v else float(v)) for d, v in zip(dates, h[c].tolist())} for c in cols}


# ============================================================================ IV rank / calendars
def iv_rank_info(symbol: str) -> dict:
    """{iv30, iv30_asof, iv_rank, iv_percentile, history_days, iv_high_52w, iv_low_52w} from the
    iv_history iv30 (vol.iv_rank_current: the latest stored IV30 and its date; rank of that value over
    the last 252 rows, only when it is from iv_session). Every page shows this iv30 as the current IV30."""
    h = iv_history(symbol, ["iv30"])
    return vol.iv_rank_current(_dates(h["date"]), h["iv30"].tolist(), iv_session())


def iv_rank_all() -> dict[str, dict]:
    """iv_rank_info for every symbol with IV history (one query, last 252 rows; cached per sweep)."""
    def build():
        session = iv_session()
        return {s: vol.iv_rank_current(_dates(c["date"]), c["iv30"].tolist(), session)
                for s, c in vol_history_repo.iv30_bulk(vol.IV_RANK_ROWS).items()}
    return _memoized("iv_rank_all", build)


def next_earnings_map() -> dict[str, EarningsEvent]:
    """{symbol: first earnings event on/after today} (cached per sweep)."""
    return _memoized("next_earnings", lambda: earn_repo.next_events(today()))


def next_exdiv_map() -> dict[str, Dividend]:
    """{symbol: first ex-dividend on/after today} (cached per sweep)."""
    return _memoized("next_exdiv", lambda: div_repo.next_ex(today()))


EVENT_VAR_YEARS = 4


def universe_event_variance() -> float | None:
    """Median one-day earnings event variance across universe symbols with enough stored events
    (analytics.earnings.universe_event_variance; the model fallback for symbols with too few prior
    events). Uses all stored past events, not only those before a given trade (cached per sweep)."""
    def build():
        t = today()
        start = t - timedelta(days=round(365.25 * EVENT_VAR_YEARS))
        by: dict[str, list[EarningsEvent]] = {}
        for e in earn_repo.between(start, t - timedelta(days=1), symbols_repo.universe_symbols()):
            by.setdefault(e.symbol, []).append(e)
        syms = [s for s, evs in by.items() if len(evs) >= earn_a.MIN_EVENTS_FOR_VAR]
        bars = bars_repo.bars_since(start - timedelta(days=14), syms) if syms else {}
        return earn_a.universe_event_variance(
            [m for _, m in earn_a.reaction_log_moves(bar_arrays_from_columns(bars[s]), by[s])]
            for s in syms if s in bars)
    return _memoized("universe_event_var", build)


def expected_reaction_day(ev: EarningsEvent) -> date:
    """Reaction session of a future event as known today: bmo -> event day, amc/unknown -> next
    session (unknown counts as amc, so windows include the event), on the listing's exchange calendar."""
    return earn_a.reaction_window(ev.date, ev.timing, earn_a.event_calendar(ev))[1]


def next_earnings_date(symbol: str) -> date | None:
    ev = next_earnings_map().get(normalize_symbol(symbol))
    return ev.date if ev else None


# ============================================================================ chain arrays
def _root(osi: str) -> str:
    return osi[:-15]


def dedupe_roots(cols: dict[str, np.ndarray], extra_key: str | None = None) -> dict[str, np.ndarray]:
    """One root per expiry (per ``extra_key`` value too, e.g. EOD date): where a PM-settled weekly
    root (SPXW, NDXP, RUTW) lists an expiry, rows of the AM root (SPX, NDX, RUT) for that expiry are
    dropped — this removes identical (expiry, strike, right) duplicates and avoids mixing AM- and
    PM-settled prices within one expiry. Columns must include osi and expiry."""
    osi = cols["osi"]
    if len(osi) == 0:
        return cols
    is_pm = np.array([_root(o) in PM_ROOTS for o in osi])
    if not is_pm.any() or is_pm.all():
        return cols
    group = cols["expiry"].astype("datetime64[D]").astype(np.int64)
    if extra_key:
        group = group * 100_000 + cols[extra_key].astype("datetime64[D]").astype(np.int64)
    keep = is_pm | ~np.isin(group, group[is_pm])
    return {c: a[keep] for c, a in cols.items()}


_SRC_FIELDS = ("osi", "expiry", "strike", "right", "bid", "ask", "last", "change", "volume", "oi", "iv",
               "delta", "gamma", "theta", "vega", "rho", "theo", "last_ts", "iv_source", "greeks_source")


def num(v: Any) -> float | None:
    """Finite float or None (None/NaN/inf -> None)."""
    if v is None:
        return None
    f = float(v)
    return f if np.isfinite(f) else None


def count(v: Any) -> int | None:
    """Integer count (volume, OI) or None when unknown (None/NaN): unknown is never 0."""
    f = num(v)
    return None if f is None else int(f)


def _contracts(symbol: str, cols: dict[str, np.ndarray], expiries: list[date]) -> list[OptionContract]:
    g = {f: cols[f].tolist() for f in _SRC_FIELDS if f not in ("expiry",)}
    out = []
    for i in range(len(expiries)):
        iv = g["iv"][i]
        out.append(OptionContract(
            osi=g["osi"][i], symbol=symbol, expiry=expiries[i], strike=float(g["strike"][i]), right=g["right"][i],
            bid=num(g["bid"][i]), ask=num(g["ask"][i]), last=num(g["last"][i]), change=num(g["change"][i]),
            volume=count(g["volume"][i]), oi=count(g["oi"][i]),
            iv=num(iv) if iv and iv == iv and iv > 0 else None,
            delta=num(g["delta"][i]), gamma=num(g["gamma"][i]), theta=num(g["theta"][i]),
            vega=num(g["vega"][i]), rho=num(g["rho"][i]), theo=num(g["theo"][i]),
            last_ts=as_utc(g["last_ts"][i]), iv_source=g["iv_source"][i], greeks_source=g["greeks_source"][i]))
    return out



def _arrays(symbol: str, cols: dict[str, np.ndarray], *, r: float, asof: datetime | None = None, q: float = 0.0,
            hv30: float | None = None,
            iv_rank: float | None = None, next_event: EarningsEvent | None = None,
            with_src: bool = False) -> ChainArrays | None:
    """ChainArrays from option_latest / option_eod columns (+ underlying; as-of = max ts unless
    given); None without a usable spot."""
    cols = dedupe_roots(cols)
    if len(cols["osi"]) == 0:
        return None
    spot = float(cols["underlying"][0])
    if not np.isfinite(spot) or spot <= 0:
        return None
    expiries = cols["expiry"].astype("datetime64[D]").astype(object).tolist()
    nan = np.full(len(expiries), np.nan)
    col = lambda k: cols[k] if k in cols else nan  # noqa: E731
    return ChainArrays.from_columns(
        expiry=expiries, strike=cols["strike"], right=cols["right"], bid=cols["bid"], ask=cols["ask"],
        last=cols["last"], volume=col("volume"), oi=col("oi"), iv=col("iv"), delta=col("delta"),
        gamma=col("gamma"), theta=col("theta"), vega=col("vega"), spot=spot,
        asof=asof or as_utc(cols["ts"].max()),
        symbol=symbol, osi=cols["osi"], src=_contracts(symbol, cols, expiries) if with_src else None,
        r=r, q=q, hv30=hv30, iv_rank=iv_rank, next_earnings=next_event.date if next_event else None,
        next_reaction=expected_reaction_day(next_event) if next_event else None, iv_source=cols.get("iv_source"))


_derived: "weakref.WeakKeyDictionary[ChainArrays, dict]" = weakref.WeakKeyDictionary()


def derived(ca: ChainArrays, key: str, fn: Callable[[ChainArrays], Any]) -> Any:
    """fn(ca) memoized on the (cached, immutable) ChainArrays object."""
    with _chain_lock:
        d = _derived.setdefault(ca, {})
        if key in d:
            return d[key]
    v = fn(ca)
    with _chain_lock:
        d[key] = v
    return v


_chains: OrderedDict[str, tuple[datetime, ChainArrays]] = OrderedDict()


def _hv30(symbol: str) -> float | None:
    m = metrics_repo.get_metrics(symbol)
    v = m.get("hv30") if m else None
    return num(v)


def load_chain_arrays(symbol: str, *, ensure_fresh: bool = True) -> ChainArrays | None:
    """The symbol's current chain as ChainArrays (with r, q, hv30, iv_rank, next_earnings and
    OptionContract `src` rows), refreshing on demand first. None when no chain is stored.
    Cached per symbol until the next fetch."""
    sym = normalize_symbol(symbol)
    if ensure_fresh:
        refresh(sym)
    fetched = options_repo.latest_fetched_at(sym)
    if fetched is None:
        return None
    with _chain_lock:
        hit = _chains.get(sym)
        if hit and hit[0] == fetched:
            _chains.move_to_end(sym)
            return hit[1]
    cols = options_repo.latest_by_symbol([sym]).get(sym)
    if cols is None:
        return None
    r = rate_for(sym)
    ca = _arrays(sym, cols, r=r, hv30=_hv30(sym), iv_rank=iv_rank_info(sym)["iv_rank"],
                 next_event=next_earnings_map().get(sym), with_src=True)
    if ca is None:
        return None
    ca = replace(ca, q=dividend_yield(sym, ca.spot, ca))
    with _chain_lock:
        _chains[sym] = (fetched, ca)
        while len(_chains) > CHAIN_CACHE_SIZE:
            _chains.popitem(last=False)
    return ca


def eod_chain_arrays(symbol: str, day: date) -> ChainArrays | None:
    """The symbol's stored end-of-day chain of `day` (as of that close), or None."""
    sym = normalize_symbol(symbol)
    cols = options_repo.eod_for_date(day, [sym])
    if not len(cols["osi"]):
        return None
    return _arrays(sym, cols, r=rate_for(sym, day), asof=close_dt(day))


def load_universe_chain_arrays(symbols: Sequence[str] | None = None, right: str | None = None,
                               dte_min: int | None = None, dte_max: int | None = None,
                               min_oi: int = 0, min_volume: int = 0) -> list[ChainArrays]:
    """ChainArrays (no `src`) for every stored chain (or `symbols`), filtered in SQL by right, DTE
    window (from today), min OI and min volume. One option query; context values in bulk."""
    t = today()
    syms = None if symbols is None else [normalize_symbol(s) for s in symbols]
    by = options_repo.latest_by_symbol(
        syms, right, t + timedelta(days=dte_min) if dte_min is not None else None,
        t + timedelta(days=dte_max) if dte_max is not None else None, min_oi or None, min_volume or None)
    if not by:
        return []
    mets = metrics_repo.metrics_on()
    ranks, earn = iv_rank_all(), next_earnings_map()
    qs = dividend_yield_map({s: float(c["underlying"][0]) for s, c in by.items()})
    out = []
    for sym, cols in by.items():
        ev = earn.get(sym)
        ca = _arrays(sym, cols, r=rate_for(sym), q=qs.get(sym, 0.0), hv30=num((mets.get(sym) or {}).get("hv30")),
                     iv_rank=(ranks.get(sym) or {}).get("iv_rank"), next_event=ev)
        if ca is not None:
            out.append(ca)
    return out


# ============================================================================ backtest prices
class _EodDay:
    """One date of a symbol's option_eod: {(expiry, right, strike): (mid, spread_pct)} + listings."""

    def __init__(self, cols: dict[str, np.ndarray], lo: int, hi: int):
        exp = cols["expiry"][lo:hi].astype("datetime64[D]").astype(object).tolist()
        k, rt = cols["strike"][lo:hi].tolist(), cols["right"][lo:hi].tolist()
        bid, ask, last = cols["bid"][lo:hi], cols["ask"][lo:hi], cols["last"][lo:hi]
        both = (bid > 0) & (ask > 0)
        mid = np.where(both, (bid + ask) / 2.0, last)
        with np.errstate(divide="ignore", invalid="ignore"):
            spread = np.where(both, (ask - bid) / mid, np.nan)
        self.quotes = {(e, r, s): (num(m), num(sp)) for e, r, s, m, sp in zip(exp, rt, k, mid, spread)}
        self.expiries = sorted(set(exp))
        self.strikes: dict[tuple[date, str], list[float]] = {}
        for e, r, s in zip(exp, rt, k):
            self.strikes.setdefault((e, r), []).append(s)


class _SymbolEod:
    """A symbol's option_eod, loaded one calendar month at a time on first use."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.dates = set(options_repo.eod_dates(symbol))
        self.days: dict[date, _EodDay] = {}
        self.months: set[tuple[int, int]] = set()

    def _load_month(self, d: date) -> None:
        first = d.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        cols = dedupe_roots(options_repo.eod_for_symbol(self.symbol, first, last), extra_key="date")
        dates = cols["date"]          # ordered by date, osi
        uniq, starts = np.unique(dates, return_index=True)
        ends = np.append(starts[1:], len(dates))
        for u, a, b in zip(uniq, starts, ends):
            self.days[as_date(u)] = _EodDay(cols, int(a), int(b))
        self.months.add((d.year, d.month))

    def day(self, d: date) -> _EodDay | None:
        if d not in self.dates:
            return None
        if (d.year, d.month) not in self.months:
            self._load_month(d)
        return self.days.get(d)


def _dates(a: np.ndarray) -> list[date]:
    """datetime64 column -> list[date] (vectorized as_date)."""
    return np.asarray(a).astype("datetime64[D]").astype(object).tolist()


class _SymbolIv:
    """Inputs of the SPEC §0.3 IV proxy for one symbol (stored IVs per iv_history)."""

    TENORS = (30, 60, 90, 120, 180, 360)

    def __init__(self, symbol: str):
        m = iv_history(symbol, [f"iv{t}" for t in self.TENORS] + ["hv30"])
        self.by_date = {d: i for i, d in enumerate(_dates(m["date"]))}
        self.iv = np.stack([m[f"iv{t}"] for t in self.TENORS], axis=1) if len(m["date"]) else np.zeros((0, 6))
        self.k = vol.iv_hv_ratio(m["iv30"], m["hv30"]) if len(m["date"]) else vol.IV_HV_DEFAULT_RATIO
        b = bars_repo.get_bars(symbol)
        self.bar_pos = {d: i for i, d in enumerate(_dates(b["date"]))}
        self.hv30 = vol.hv_series(b["close"], 30)

    def proxy(self, d: date, tenor_days: int) -> float | None:
        i = self.by_date.get(d)
        if i is not None:
            ok = np.isfinite(self.iv[i]) & (self.iv[i] > 0)
            if ok.any():
                t = np.array(self.TENORS, float)[ok] / 365.0
                return vol.interp_total_variance(t, self.iv[i][ok], tenor_days / 365.0)
        j = self.bar_pos.get(d)
        if j is None or not np.isfinite(self.hv30[j]):
            return None
        return float(self.hv30[j] * self.k)


class DbPriceProvider:
    """analytics.optbacktest.PriceProvider on stored data: real EOD mids, spreads and listed
    expiries/strikes from option_eod; IV proxy (stored constant-maturity IV interpolated to the
    tenor → HV30·k, k = median IV30/HV30, default 1.15) from iv_history and daily bars.
    Data is loaded lazily per symbol and kept for the provider's lifetime (one request)."""

    def __init__(self) -> None:
        self._eod: dict[str, _SymbolEod] = {}
        self._iv: dict[str, _SymbolIv] = {}

    def _day(self, d: date, symbol: str) -> _EodDay | None:
        if symbol not in self._eod:
            self._eod[symbol] = _SymbolEod(symbol)
        return self._eod[symbol].day(d)

    def _quote(self, d, symbol, expiry, strike, right) -> tuple[float | None, float | None] | None:
        day = self._day(d, symbol)
        return None if day is None else day.quotes.get((expiry, right, float(strike)))

    def real_mid(self, d: date, symbol: str, expiry: date, strike: float, right: str) -> float | None:
        q = self._quote(d, symbol, expiry, strike, right)
        return q[0] if q else None

    def real_spread_pct(self, d: date, symbol: str, expiry: date, strike: float, right: str) -> float | None:
        q = self._quote(d, symbol, expiry, strike, right)
        return q[1] if q else None

    def listed_expiries(self, d: date, symbol: str) -> list[date]:
        day = self._day(d, symbol)
        return day.expiries if day else []

    def listed_strikes(self, d: date, symbol: str, expiry: date, right: str) -> list[float]:
        day = self._day(d, symbol)
        return day.strikes.get((expiry, right), []) if day else []

    def iv_proxy(self, d: date, symbol: str, tenor_days: int) -> float | None:
        if symbol not in self._iv:
            self._iv[symbol] = _SymbolIv(symbol)
        return self._iv[symbol].proxy(d, tenor_days)
