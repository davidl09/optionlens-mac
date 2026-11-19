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
from typing import Any, Awaitable, Callable, Mapping, Sequence

import anyio.from_thread
import numpy as np

from ..analytics import vol
from ..analytics.calendar import NY, is_trading_day, now_ny
from ..analytics.chain import ChainArrays
from ..analytics.strategies import close_dt
from ..analytics.dividends import PARITY_BAND, PARITY_DTE, implied_yield_from_chain, trailing_yield
from ..config import get_settings
from ..jobs import hooks, ondemand
from ..models import Bar, Dividend, EarningsEvent, OptionContract, normalize_symbol
from ..repo import bars as bars_repo
from ..repo import dividends as div_repo
from ..repo import earnings as earn_repo
from ..repo import market as market_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo

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
    if row is None or not bars_repo.coverage([sym]):
        run_async(ondemand.ensure_history, sym)
        row = symbols_repo.get_symbol(sym)
    if row is None:
        raise ondemand.UnknownSymbol(sym)
    return row


REFRESH_MIN_INTERVAL_S = 120.0   # one on-demand chain attempt per symbol per interval (backoff on upstream errors)
_attempts: dict[str, float] = {}


def refresh(symbol: str) -> dict:
    """ensure_known + fetch the chain if stale (SPEC §5 on-demand), at most one attempt per symbol
    every REFRESH_MIN_INTERVAL_S (a failed upstream fetch serves stored data meanwhile).
    Returns the symbols row."""
    sym = normalize_symbol(symbol)
    row = symbols_repo.get_symbol(sym)
    last = _attempts.get(sym)
    if row is not None and last is not None and time.monotonic() - last < REFRESH_MIN_INTERVAL_S:
        return row
    run_async(ondemand.ensure_fresh, sym)
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


# ============================================================================ rates / dividends
def risk_free_rate(day: date | None = None) -> float:
    """13-week T-bill (rates table, latest on/before day) else config RISK_FREE_RATE."""
    r = market_repo.rate_on(day)
    return float(r) if r is not None else get_settings().risk_free_rate


def _index_q(symbol: str, kind: str | None) -> float | None:
    return INDEX_DEFAULT_Q.get(symbol, 0.0) if kind == "index" else None


def dividend_yield(symbol: str, spot: float | None, chain_arrays: ChainArrays | None = None,
                   day: date | None = None) -> float:
    """Continuous q: indices by default table; symbols with dividend history: trailing-12m / spot;
    otherwise (Nasdaq has no history for NYSE/ARCA listings) implied by put-call parity on the
    chain (0 when not estimable)."""
    sym = normalize_symbol(symbol)
    row = symbols_repo.get_symbol(sym)
    q = _index_q(sym, row and row["kind"])
    if q is not None:
        return q
    divs = div_repo.for_symbol(sym)
    if divs:
        return trailing_yield(divs, spot, day or today())
    ca = chain_arrays if chain_arrays is not None else load_chain_arrays(sym, ensure_fresh=False)
    if ca is None:
        return 0.0
    return implied_yield_from_chain(ca, risk_free_rate(day)) or 0.0


def _implied_yields_all() -> dict[str, float]:
    """Parity-implied q for every stored chain (near-ATM rows only; one query)."""
    t, r = today(), risk_free_rate()
    near = options_repo.near_atm_by_symbol(t + timedelta(days=PARITY_DTE[0]), t + timedelta(days=PARITY_DTE[1]),
                                           PARITY_BAND)
    out = {}
    for sym, cols in near.items():
        ca = _arrays(sym, cols, r=r, with_src=False)
        q = implied_yield_from_chain(ca, r) if ca is not None else None
        if q is not None:
            out[sym] = q
    return out


def dividend_yield_map(spots: Mapping[str, float]) -> dict[str, float]:
    """Bulk dividend_yield for {symbol: spot} (no per-symbol queries)."""
    def build():
        kinds = {r["symbol"]: r["kind"] for r in symbols_repo.list_symbols()}
        return kinds, div_repo.symbols_with_history(), div_repo.trailing_sum(today())
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


def rate_fn() -> Callable[[date], float]:
    """r(d): stored 13-week T-bill on/before d (config fallback) — for backtests (cached per sweep)."""
    def build():
        rows = market_repo.rates_between(date(1900, 1, 1), date(2999, 12, 31))
        return [r["date"] for r in rows], [float(r["rate"]) for r in rows]
    days, rates = _memoized("rates", build)
    fallback = get_settings().risk_free_rate

    def r(d: date) -> float:
        i = bisect.bisect_right(days, d) - 1
        return rates[i] if i >= 0 else (rates[0] if rates else fallback)
    return r


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
        d = q.ts.astimezone(NY).date() if q and q.ts else None
        if q and q.price and d and is_trading_day(d) and (not out or out[-1].date < d) \
                and (start is None or d >= start):
            out.append(Bar(sym, d, q.open, q.high, q.low, float(q.price), q.volume))
    return out


# ============================================================================ quote / metrics
def quote_dict(symbol: str) -> dict | None:
    q = quotes_repo.get_quote(symbol)
    if q is None or q.price is None:
        return None
    return {f: getattr(q, f) for f in ("symbol", "ts", "price", "change", "change_pct", "open", "high", "low",
                                        "prev_close", "volume", "bid", "ask", "iv30")}


def metrics_series(symbol: str, cols: list[str]) -> dict[str, dict[date, float | None]]:
    """{col: {date: value}} of the symbol's stored metrics (NaN -> None)."""
    h = metrics_repo.history(symbol, cols=cols)
    dates = [as_date(d) for d in h["date"]]
    return {c: {d: (None if v != v else float(v)) for d, v in zip(dates, h[c].tolist())} for c in cols}


# ============================================================================ IV rank / calendars
def iv_rank_info(symbol: str) -> dict:
    """{iv_rank, iv_percentile, history_days, iv_high_52w, iv_low_52w} from stored iv30 history."""
    h = metrics_repo.history(normalize_symbol(symbol), cols=["iv30"])
    return vol.iv_rank_percentile(h["iv30"].tolist())


def iv_rank_all() -> dict[str, dict]:
    """iv_rank_info for every symbol with stored metrics (one query; cached per sweep)."""
    def build():
        return {s: vol.iv_rank_percentile(c["iv30"].tolist())
                for s, c in metrics_repo.history_bulk(["iv30"], last_n=252).items()}
    return _memoized("iv_rank_all", build)


def next_earnings_map() -> dict[str, EarningsEvent]:
    """{symbol: first earnings event on/after today} (cached per sweep)."""
    return _memoized("next_earnings", lambda: earn_repo.next_events(today()))


def next_exdiv_map() -> dict[str, Dividend]:
    """{symbol: first ex-dividend on/after today} (cached per sweep)."""
    return _memoized("next_exdiv", lambda: div_repo.next_ex(today()))


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
               "delta", "gamma", "theta", "vega", "rho", "theo")


def num(v: Any) -> float | None:
    """Finite float or None (None/NaN/inf -> None)."""
    if v is None:
        return None
    f = float(v)
    return f if np.isfinite(f) else None


def _contracts(symbol: str, cols: dict[str, np.ndarray], expiries: list[date]) -> list[OptionContract]:
    g = {f: cols[f].tolist() for f in _SRC_FIELDS if f not in ("expiry",)}
    out = []
    for i in range(len(expiries)):
        iv = g["iv"][i]
        out.append(OptionContract(
            osi=g["osi"][i], symbol=symbol, expiry=expiries[i], strike=float(g["strike"][i]), right=g["right"][i],
            bid=num(g["bid"][i]), ask=num(g["ask"][i]), last=num(g["last"][i]), change=num(g["change"][i]),
            volume=int(num(g["volume"][i]) or 0), oi=int(num(g["oi"][i]) or 0),
            iv=num(iv) if iv and iv == iv and iv > 0 else None,
            delta=num(g["delta"][i]), gamma=num(g["gamma"][i]), theta=num(g["theta"][i]),
            vega=num(g["vega"][i]), rho=num(g["rho"][i]), theo=num(g["theo"][i])))
    return out


def _arrays(symbol: str, cols: dict[str, np.ndarray], *, r: float, asof: datetime | None = None, q: float = 0.0,
            hv30: float | None = None,
            iv_rank: float | None = None, next_earnings: date | None = None,
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
        r=r, q=q, hv30=hv30, iv_rank=iv_rank, next_earnings=next_earnings)


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
    r = risk_free_rate()
    ca = _arrays(sym, cols, r=r, hv30=_hv30(sym), iv_rank=iv_rank_info(sym)["iv_rank"],
                 next_earnings=next_earnings_date(sym), with_src=True)
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
    return _arrays(sym, cols, r=risk_free_rate(day), asof=close_dt(day))


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
    r = risk_free_rate()
    mets = metrics_repo.metrics_on()
    ranks, earn = iv_rank_all(), next_earnings_map()
    qs = dividend_yield_map({s: float(c["underlying"][0]) for s, c in by.items()})
    out = []
    for sym, cols in by.items():
        ev = earn.get(sym)
        ca = _arrays(sym, cols, r=r, q=qs.get(sym, 0.0), hv30=num((mets.get(sym) or {}).get("hv30")),
                     iv_rank=(ranks.get(sym) or {}).get("iv_rank"), next_earnings=ev.date if ev else None)
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


class _SymbolIv:
    """Inputs of the SPEC §0.3 IV proxy for one symbol."""

    TENORS = (30, 60, 90, 120, 180, 360)

    def __init__(self, symbol: str):
        m = metrics_repo.history(symbol, cols=[f"iv{t}" for t in self.TENORS] + ["hv30"])
        self.by_date = {as_date(d): i for i, d in enumerate(m["date"])}
        self.iv = np.stack([m[f"iv{t}"] for t in self.TENORS], axis=1) if len(m["date"]) else np.zeros((0, 6))
        self.k = vol.iv_hv_ratio(m["iv30"], m["hv30"]) if len(m["date"]) else vol.IV_HV_DEFAULT_RATIO
        b = bars_repo.get_bars(symbol)
        self.bar_pos = {as_date(d): i for i, d in enumerate(b["date"])}
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
    tenor → HV30·k, k = median IV30/HV30, default 1.15) from metrics_daily and daily bars.
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
