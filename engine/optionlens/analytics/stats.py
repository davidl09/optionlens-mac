"""Stock price statistics (SPEC §6): returns, beta/correlation, RSI, ATR, SMAs, highs/lows,
support/resistance, relative volume, price-action table, intraday/overnight decomposition,
seasonality, gaps, volatility seasonality.

Pure functions over daily bars (sorted ascending by date). Missing OHLC/volume values are NaN
internally; outputs use None for anything not computable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

import numpy as np

from optionlens.models import Bar

RETURN_WINDOWS: dict[str, int] = {"d1": 1, "d5": 5, "m1": 21, "m3": 63, "m6": 126, "y1": 252, "y3": 756}


# ---------------------------------------------------------------------------------------------
# Bars as arrays
# ---------------------------------------------------------------------------------------------
def _f(x: float | None) -> float:
    return float("nan") if x is None else float(x)


@dataclass(frozen=True, slots=True)
class BarArrays:
    """Column view of a bar series (ascending dates). NaN where a bar field is None."""

    dates: tuple[date, ...]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    pos: Mapping[date, int] = field(repr=False)
    ords: np.ndarray = field(repr=False)

    def __len__(self) -> int:
        return len(self.dates)

    def index_of(self, d: date) -> int | None:
        return self.pos.get(d)

    def index_before(self, d: date) -> int | None:
        """Index of the last bar strictly before d."""
        i = int(np.searchsorted(self.ords, d.toordinal(), side="left")) - 1
        return i if i >= 0 else None

    def index_on_or_after(self, d: date) -> int | None:
        i = int(np.searchsorted(self.ords, d.toordinal(), side="left"))
        return i if i < len(self.dates) else None


def bar_arrays(bars: Sequence[Bar]) -> BarArrays:
    bs = sorted(bars, key=lambda b: b.date)
    dates = tuple(b.date for b in bs)
    return BarArrays(
        dates=dates,
        open=np.array([_f(b.open) for b in bs], dtype=float),
        high=np.array([_f(b.high) for b in bs], dtype=float),
        low=np.array([_f(b.low) for b in bs], dtype=float),
        close=np.array([float(b.close) for b in bs], dtype=float),
        volume=np.array([_f(b.volume) for b in bs], dtype=float),
        pos={d: i for i, d in enumerate(dates)},
        ords=np.array([d.toordinal() for d in dates], dtype=np.int64),
    )


def bar_arrays_from_columns(cols: Mapping[str, np.ndarray]) -> BarArrays:
    """BarArrays from repo columns {date (datetime64[D], ascending), open, high, low, close, volume}."""
    dates = tuple(np.asarray(cols["date"]).astype("datetime64[D]").astype(object).tolist())
    f = lambda k: np.asarray(cols[k], dtype=float)  # noqa: E731
    return BarArrays(dates=dates, open=f("open"), high=f("high"), low=f("low"), close=f("close"),
                     volume=f("volume"), pos={d: i for i, d in enumerate(dates)},
                     ords=np.array([d.toordinal() for d in dates], dtype=np.int64))


def _num(x: float) -> float | None:
    """NaN/inf -> None, else float."""
    return float(x) if x is not None and math.isfinite(x) else None


def _mean(xs: Sequence[float]) -> float | None:
    return float(np.mean(xs)) if len(xs) else None


# ---------------------------------------------------------------------------------------------
# Returns, beta, indicators
# ---------------------------------------------------------------------------------------------
def returns_row(dates: Sequence[date], closes: np.ndarray) -> dict[str, float | None]:
    """ReturnsRow: close[-1]/close[-1-n] − 1 for each trading-day window; ytd vs last close of
    the prior calendar year. None when history is too short."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out: dict[str, float | None] = {}
    for key, w in RETURN_WINDOWS.items():
        out[key] = _num(closes[-1] / closes[-1 - w] - 1.0) if n > w else None
    out["ytd"] = None
    if n:
        year = dates[-1].year
        prior = [i for i in range(n - 1, -1, -1) if dates[i].year < year]
        if prior:
            out["ytd"] = _num(closes[-1] / closes[prior[0]] - 1.0)
    return {k: out[k] for k in ("d1", "d5", "m1", "m3", "m6", "ytd", "y1", "y3")}


def beta_corr(
    dates: Sequence[date], closes: np.ndarray, bench_dates: Sequence[date], bench_closes: np.ndarray,
    window: int = 252, min_obs: int = 20,
) -> tuple[float | None, float | None]:
    """(beta, correlation) of daily log returns vs a benchmark over the last `window` returns on
    common dates."""
    bmap = {d: float(c) for d, c in zip(bench_dates, bench_closes)}
    common = [(float(c), bmap[d]) for d, c in zip(dates, closes) if d in bmap]
    if len(common) < 2:
        return None, None
    arr = np.array(common[-(window + 1):], dtype=float)
    a = np.diff(np.log(arr[:, 0]))
    b = np.diff(np.log(arr[:, 1]))
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if len(a) < min_obs:
        return None, None
    vb = float(np.var(b, ddof=1))
    va = float(np.var(a, ddof=1))
    if vb <= 0:
        return None, None
    cov = float(np.cov(a, b, ddof=1)[0, 1])
    corr = cov / math.sqrt(va * vb) if va > 0 else None
    return cov / vb, corr


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder smoothing: first value = mean of the first n inputs, then (prev·(n−1)+x)/n."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def rsi_series(closes: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder RSI, aligned with closes (NaN for the first n closes)."""
    closes = np.asarray(closes, dtype=float)
    out = np.full(len(closes), np.nan)
    if len(closes) <= n:
        return out
    d = np.diff(closes)
    ag = _wilder(np.where(d > 0, d, 0.0), n)
    al = _wilder(np.where(d < 0, -d, 0.0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(al == 0, np.where(ag == 0, 50.0, 100.0), 100.0 - 100.0 / (1.0 + ag / al))
    out[1:] = np.where(np.isnan(ag), np.nan, rsi)
    return out


def rsi(closes: np.ndarray, n: int = 14) -> float | None:
    s = rsi_series(closes, n)
    return _num(s[-1]) if len(s) else None


def atr_pct(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> float | None:
    """Wilder ATR(n) / last close. Missing high/low fall back to close."""
    close = np.asarray(close, dtype=float)
    if len(close) <= n:
        return None
    h = np.where(np.isfinite(high), high, close)
    lo = np.where(np.isfinite(low), low, close)
    pc = close[:-1]
    tr = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - pc), np.abs(lo[1:] - pc)])
    atr = _wilder(tr, n)
    return _num(atr[-1] / close[-1])


def sma(closes: np.ndarray, n: int) -> float | None:
    closes = np.asarray(closes, dtype=float)
    return float(np.mean(closes[-n:])) if len(closes) >= n else None


def sma_series(closes: np.ndarray, n: int) -> np.ndarray:
    closes = np.asarray(closes, dtype=float)
    out = np.full(len(closes), np.nan)
    if len(closes) >= n:
        c = np.concatenate([[0.0], np.cumsum(closes)])
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def high_low(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> tuple[float | None, float | None]:
    """(max high, min low) over the last n bars (fallback to closes where high/low missing)."""
    close = np.asarray(close, dtype=float)
    if not len(close):
        return None, None
    h = np.where(np.isfinite(high), high, close)[-n:]
    lo = np.where(np.isfinite(low), low, close)[-n:]
    return float(np.max(h)), float(np.min(lo))


def avg_volume(volume: np.ndarray, n: int = 30, exclude_last: bool = False) -> float | None:
    v = np.asarray(volume, dtype=float)
    if exclude_last:
        v = v[:-1]
    v = v[-n:]
    v = v[np.isfinite(v)]
    return float(np.mean(v)) if len(v) else None


def relative_volume(volume: np.ndarray, n: int = 30) -> float | None:
    """Last volume / average of the prior n volumes."""
    v = np.asarray(volume, dtype=float)
    if len(v) < 2 or not np.isfinite(v[-1]):
        return None
    avg = avg_volume(v, n, exclude_last=True)
    return float(v[-1] / avg) if avg else None


OPTION_VOLUME_AVG_DAYS = 20


def prior_option_volume_avg(call_volume: np.ndarray, put_volume: np.ndarray,
                            n: int = OPTION_VOLUME_AVG_DAYS) -> tuple[float | None, int]:
    """(mean daily option volume, days used) over up to the last n PRIOR stored days (pass history
    without today). A day counts when either side is known. (None, 0) without any prior day — an
    average is never fabricated; callers flag the day count (fewer than n days = partial average)."""
    cv, pv = np.asarray(call_volume, dtype=float), np.asarray(put_volume, dtype=float)
    known = np.isfinite(cv) | np.isfinite(pv)
    tot = (np.nan_to_num(cv) + np.nan_to_num(pv))[known][-n:]
    return (float(np.mean(tot)), len(tot)) if len(tot) else (None, 0)


def support_resistance(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, lookback: int = 126,
    cluster_pct: float = 0.015, count: int = 3,
) -> tuple[list[float], list[float]]:
    """(support, resistance) from 5-bar fractal pivots over the lookback.

    A pivot high is a bar whose high is strictly above the two highs before and after it (pivot
    low mirrors). All pivot prices are pooled, sorted, and grouped while within `cluster_pct` of
    the group's first level; a group's level is its mean. Support = nearest `count` levels below
    the last close (nearest first); resistance = nearest `count` above (nearest first)."""
    close = np.asarray(close, dtype=float)
    if not len(close):
        return [], []
    h = np.where(np.isfinite(high), high, close)[-lookback:]
    lo = np.where(np.isfinite(low), low, close)[-lookback:]
    levels: list[float] = []
    for i in range(2, len(h) - 2):
        if h[i] > max(h[i - 2], h[i - 1], h[i + 1], h[i + 2]):
            levels.append(float(h[i]))
        if lo[i] < min(lo[i - 2], lo[i - 1], lo[i + 1], lo[i + 2]):
            levels.append(float(lo[i]))
    levels.sort()
    clusters: list[list[float]] = []
    for lv in levels:
        if clusters and lv <= clusters[-1][0] * (1 + cluster_pct):
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    centers = [float(np.mean(c)) for c in clusters]
    px = float(close[-1])
    support = sorted((c for c in centers if c < px), reverse=True)[:count]
    resistance = sorted(c for c in centers if c > px)[:count]
    return support, resistance


# ---------------------------------------------------------------------------------------------
# Gaps, price action
# ---------------------------------------------------------------------------------------------
def gap_signal(bars: Sequence[Bar]) -> dict[str, float | bool | None]:
    """Latest bar's gap: {gap_pct = open/prev close − 1, continued = close moved further in the
    gap direction (close > open for gap up, close < open for gap down)}."""
    a = bar_arrays(bars)
    if len(a) < 2 or not np.isfinite(a.open[-1]):
        return {"gap_pct": None, "continued": None}
    g = float(a.open[-1] / a.close[-2] - 1.0)
    cont = (a.close[-1] > a.open[-1]) if g > 0 else (a.close[-1] < a.open[-1]) if g < 0 else False
    return {"gap_pct": g, "continued": bool(cont)}


def _price_action_row(a: BarArrays, i: int, rel_n: int = 30) -> dict:
    c = a.close[i]
    pc = a.close[i - 1] if i > 0 else float("nan")
    o = a.open[i] if np.isfinite(a.open[i]) else c
    h = a.high[i] if np.isfinite(a.high[i]) else max(o, c)
    lo = a.low[i] if np.isfinite(a.low[i]) else min(o, c)
    base = pc if np.isfinite(pc) else o
    rel = None
    if i >= rel_n and np.isfinite(a.volume[i]):
        prior = a.volume[i - rel_n:i]
        prior = prior[np.isfinite(prior)]
        if len(prior) and np.mean(prior) > 0:
            rel = float(a.volume[i] / np.mean(prior))
    return {
        "date": a.dates[i], "open": float(o), "high": float(h), "low": float(lo), "close": float(c),
        "volume": int(a.volume[i]) if np.isfinite(a.volume[i]) else 0,
        "change_pct": _num(c / pc - 1.0) if i > 0 else None,
        "gap_pct": _num(a.open[i] / pc - 1.0) if i > 0 and np.isfinite(a.open[i]) else None,
        "range_pct": float((h - lo) / base) if base else 0.0,
        "rel_volume": rel,
    }


def price_action(bars: Sequence[Bar], start: date | None = None) -> dict:
    """PriceAction for bars with date ≥ start (indicators use the full history).

    range_pct = (high − low)/prev close (open for the first bar); rel_volume = volume / mean of
    the prior 30 volumes (None with < 30 prior bars). rows newest first."""
    a = bar_arrays(bars)
    idx = [i for i in range(len(a)) if start is None or a.dates[i] >= start]
    rows = [_price_action_row(a, i) for i in idx]
    chg = [r["change_pct"] for r in rows if r["change_pct"] is not None]
    gaps = [abs(r["gap_pct"]) for r in rows if r["gap_pct"] is not None]
    with_chg = [r for r in rows if r["change_pct"] is not None]
    stats = {
        "days": len(rows),
        "avg_abs_move": _mean([abs(x) for x in chg]),
        "up_day_pct": _mean([1.0 if x > 0 else 0.0 for x in chg]),
        "avg_gap_abs": _mean(gaps),
        "largest_up": max(with_chg, key=lambda r: r["change_pct"]) if with_chg else None,
        "largest_down": min(with_chg, key=lambda r: r["change_pct"]) if with_chg else None,
        "stdev_daily": float(np.std(chg, ddof=1)) if len(chg) > 1 else None,
    }
    if stats["largest_up"] is not None and stats["largest_up"]["change_pct"] <= 0:
        stats["largest_up"] = None
    if stats["largest_down"] is not None and stats["largest_down"]["change_pct"] >= 0:
        stats["largest_down"] = None
    return {"rows": rows[::-1], "stats": stats, "intraday_overnight": intraday_overnight(a, idx)}


def intraday_overnight(a: BarArrays, idx: Sequence[int] | None = None) -> dict:
    """overnight = open/prev close − 1, intraday = close/open − 1 (bars with an open and a prior
    close); cumulative = compounded; series chronological."""
    idx = range(len(a)) if idx is None else idx
    dates, on, intra = [], [], []
    for i in idx:
        if i > 0 and np.isfinite(a.open[i]) and a.open[i] > 0:
            dates.append(a.dates[i])
            on.append(a.open[i] / a.close[i - 1] - 1.0)
            intra.append(a.close[i] / a.open[i] - 1.0)
    on_a, in_a = np.array(on), np.array(intra)
    cum_on = np.cumprod(1 + on_a) - 1 if len(on_a) else on_a
    cum_in = np.cumprod(1 + in_a) - 1 if len(in_a) else in_a
    return {
        "avg_overnight": _mean(on), "avg_intraday": _mean(intra),
        "cum_overnight": float(cum_on[-1]) if len(on) else None,
        "cum_intraday": float(cum_in[-1]) if len(on) else None,
        "overnight_up_pct": _mean([1.0 if x > 0 else 0.0 for x in on]),
        "intraday_up_pct": _mean([1.0 if x > 0 else 0.0 for x in intra]),
        "series": [{"date": d, "cum_overnight": float(x), "cum_intraday": float(y)}
                   for d, x, y in zip(dates, cum_on, cum_in)],
    }


# ---------------------------------------------------------------------------------------------
# Seasonality
# ---------------------------------------------------------------------------------------------
def monthly_returns(dates: Sequence[date], closes: np.ndarray) -> dict[tuple[int, int], float]:
    """{(year, month): last close of month / last close of prior month − 1} for every month whose
    prior month also has a close."""
    last: dict[tuple[int, int], float] = {}
    for d, c in zip(dates, closes):
        last[(d.year, d.month)] = float(c)
    out = {}
    for (y, m), c in last.items():
        prev = (y, m - 1) if m > 1 else (y - 1, 12)
        if prev in last:
            out[(y, m)] = c / last[prev] - 1.0
    return out


def _bucket_stats(xs: Sequence[float]) -> dict:
    return {
        "avg": _mean(xs),
        "median": float(np.median(xs)) if xs else None,
        "pos_pct": _mean([1.0 if x > 0 else 0.0 for x in xs]),
        "n": len(xs),
    }


def seasonality(bars: Sequence[Bar], today: date, years: int = 10) -> dict:
    """Seasonality shape (without symbol): years today.year−years+1 … today.year; the current
    (incomplete) month is excluded; weekday stats use daily close-to-close returns (0=Mon)."""
    a = bar_arrays(bars)
    first_year = today.year - years + 1
    mr = {k: v for k, v in monthly_returns(a.dates, a.close).items()
          if k[0] >= first_year and k != (today.year, today.month) and k <= (today.year, today.month)}
    yrs = sorted({y for y, _ in mr})
    monthly = [{"year": y, "month": m, "ret": mr.get((y, m))} for y in yrs for m in range(1, 13)]
    by_month = []
    for m in range(1, 13):
        xs = [v for (y, mm), v in mr.items() if mm == m]
        s = _bucket_stats(xs)
        by_month.append({"month": m, **s})
    wd: dict[int, list[float]] = {i: [] for i in range(5)}
    for i in range(1, len(a)):
        d = a.dates[i]
        if d.year >= first_year and d.weekday() < 5:
            wd[d.weekday()].append(a.close[i] / a.close[i - 1] - 1.0)
    by_weekday = []
    for w in range(5):
        s = _bucket_stats(wd[w])
        by_weekday.append({"weekday": w, "avg": s["avg"], "pos_pct": s["pos_pct"], "n": s["n"]})
    return {"years": yrs, "monthly": monthly, "by_month": by_month, "by_weekday": by_weekday,
            "current_month": today.month}


def vol_seasonality(iv30_by_date: Mapping[date, float | None], bars: Sequence[Bar], today: date,
                    years: int = 10) -> list[dict]:
    """VolSeasonality.months: avg stored IV30 per calendar month (iv_samples = count) and avg
    HV20 per month over the last `years` years of price history (hv_years = distinct years)."""
    from optionlens.analytics.vol import hv_series

    a = bar_arrays(bars)
    hv20 = hv_series(a.close, 20) if len(a) else np.array([])
    first_year = today.year - years + 1
    out = []
    for m in range(1, 13):
        ivs = [float(v) for d, v in iv30_by_date.items() if d.month == m and v is not None and math.isfinite(v)]
        hvs = [(a.dates[i].year, float(hv20[i])) for i in range(len(a))
               if a.dates[i].month == m and a.dates[i].year >= first_year and np.isfinite(hv20[i])]
        out.append({
            "month": m, "avg_iv30": _mean(ivs), "iv_samples": len(ivs),
            "avg_hv20": _mean([v for _, v in hvs]), "hv_years": len({y for y, _ in hvs}),
        })
    return out
