"""Backtest result assembly (shared) and the stock rule backtester (SPEC §9 /backtest/stock).

`build_result` turns trades + an equity curve into the BacktestResult shape; the options
backtesters reuse it. Pure functions.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Mapping, Sequence

import numpy as np

from optionlens.analytics import calendar as cal
from optionlens.analytics.earnings import UNKNOWN_TIMING_NOTE, event_calendar, reaction_window
from optionlens.analytics.stats import BarArrays, bar_arrays, rsi_series, sma_series
from optionlens.models import Bar, EarningsEvent

TRADING_DAYS = 252
_FLAT_STD = 1e-12


# ---------------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------------
def _histogram(rets: list[float]) -> list[dict]:
    if not rets:
        return []
    n_bins = min(20, max(5, math.ceil(math.sqrt(len(rets)))))
    lo, hi = min(rets), max(rets)
    if hi - lo < 1e-12:
        lo, hi = lo - 0.005, hi + 0.005
    counts, edges = np.histogram(rets, bins=n_bins, range=(lo, hi))
    return [{"bin_start": float(edges[i]), "bin_end": float(edges[i + 1]), "count": int(counts[i])}
            for i in range(n_bins)]


def equity_stats(equity: Sequence[tuple[date, float]]) -> dict:
    """{cagr, sharpe, max_drawdown} of a daily equity curve (see build_result)."""
    out = {"cagr": None, "sharpe": None, "max_drawdown": 0.0}
    if not equity:
        out["max_drawdown"] = None
        return out
    v = np.array([x for _, x in equity], dtype=float)
    span = (equity[-1][0] - equity[0][0]).days
    if span >= 1 and v[0] > 0 and v[-1] > 0:
        out["cagr"] = float((v[-1] / v[0]) ** (365.25 / span) - 1.0)
    if len(v) >= 3 and np.all(v > 0):          # a wiped-out account (equity ≤ 0) has no Sharpe ratio
        r = np.diff(np.log(v))                   # log returns: Σ = log(end/start), so the sign matches the total return
        sd = float(np.std(r, ddof=1))
        if sd > _FLAT_STD * max(1.0, abs(float(np.mean(r)))):
            out["sharpe"] = float(np.mean(r) / sd * math.sqrt(TRADING_DAYS))
    peak = np.maximum.accumulate(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, v / peak - 1.0, 0.0)
    out["max_drawdown"] = float(max(-1.0, min(0.0, np.min(dd))))
    return out


def build_result(
    trades: list[dict], equity: list[tuple[date, float]],
    benchmark: list[tuple[date, float]] | None, notes: list[str],
) -> dict:
    """BacktestResult. win_rate = share of trades with pnl > 0; avg/median over non-None ret;
    total_return = equity end/start − 1 (so it agrees with the equity curve and CAGR also when
    trades of several symbols overlap; for one symbol's sequential trades = Π(1+ret)−1); cagr from the
    equity curve's first/last value and calendar span; sharpe = mean/std(ddof=1)·√252 of daily
    log equity changes (its sign is the total return's; None when < 2 changes, flat or equity ≤ 0);
    max_drawdown in [−1, 0]; starting_capital = the equity curve's first value and total_pnl its dollar
    change (so P/L and total return agree); avg_hold_days in trading days."""
    rets = [t["ret"] for t in trades if t.get("ret") is not None]
    total = float(equity[-1][1] / equity[0][1] - 1.0) if trades and equity and equity[0][1] else None
    es = equity_stats(equity)
    holds = [len(cal.trading_days(t["entry_date"], t["exit_date"])) - 1 for t in trades]
    summary = {
        "count": len(trades),
        "win_rate": float(np.mean([t["pnl"] > 0 for t in trades])) if trades else None,
        "avg_return": float(np.mean(rets)) if rets else None,
        "median_return": float(np.median(rets)) if rets else None,
        "total_return": total,
        "cagr": es["cagr"], "sharpe": es["sharpe"], "max_drawdown": es["max_drawdown"],
        "avg_hold_days": float(np.mean(holds)) if holds else None,
        "starting_capital": float(equity[0][1]) if trades and equity else None,
        "total_pnl": float(equity[-1][1] - equity[0][1]) if trades and equity else None,
        "real_count": sum(1 for t in trades if t.get("pricing") == "real"),
        "model_count": sum(1 for t in trades if t.get("pricing") == "model"),
    }
    return {
        "trades": trades,
        "summary": summary,
        "equity": [{"date": d, "value": float(v)} for d, v in equity],
        "benchmark": None if benchmark is None else [{"date": d, "value": float(v)} for d, v in benchmark],
        "histogram": _histogram(rets),
        "notes": list(notes),
    }


# ---------------------------------------------------------------------------------------------
# Stock rule backtester
# ---------------------------------------------------------------------------------------------
def _cross(cond: np.ndarray) -> np.ndarray:
    """True where cond becomes true (was false on the previous bar)."""
    prev = np.concatenate([[True], cond[:-1]])
    return cond & ~prev


def _reactions(events: Sequence[EarningsEvent]) -> list[tuple[date, date]]:
    """(first, last) possible reaction sessions per event, known before the event (no look-ahead;
    see earnings.reaction_window)."""
    return sorted({reaction_window(ev.date, ev.timing, event_calendar(ev)) for ev in events})


def entry_signals(a: BarArrays, entry: Mapping, reactions: Sequence[tuple[date, date]], lo: int) -> np.ndarray:
    """Boolean entry signal per bar (only bars with index ≥ lo can fire).

    Rules (signal evaluated at the close; entry at that close):
      every_n_days n: bars lo, lo+n, lo+2n, …
      day_of_month day: first bar of each month whose day-of-month ≥ day
      month_of_year month[, day]: first bar of that month with day-of-month ≥ day (default 1)
      days_before_earnings n (default 1): bar add_trading_days(first possible reaction day, −n)
      days_after_earnings n (default 0): bar add_trading_days(last possible reaction day, n)
      (reactions = (first, last) windows; they differ only for unknown timing)
      gap direction up|down value x: open/prev close − 1 ≥ x (up) or ≤ −x (down)
      rsi direction below|above value v [n=14]: RSI crosses below v (RSI<v today, ≥v yesterday)
          or above v (RSI>v today, ≤v yesterday)
      sma_cross n direction above|below: close crosses above (close>SMA today, ≤ yesterday) or below
      new_52w direction high|low: close > max (or < min) of the prior 252 closes
    """
    n_bars = len(a)
    sig = np.zeros(n_bars, dtype=bool)
    t = entry["type"]
    c = a.close
    if t == "every_n_days":
        step = max(1, int(entry.get("n") or 1))
        sig[lo::step] = True
    elif t in ("day_of_month", "month_of_year"):
        day = int(entry.get("day") or 1)
        month = entry.get("month")
        if t == "month_of_year" and month is None:
            raise ValueError("month_of_year needs entry.month")
        seen: set[tuple[int, int]] = set()
        for i in range(lo, n_bars):
            d = a.dates[i]
            if (t == "month_of_year" and d.month != int(month)) or d.day < day:
                continue
            if (d.year, d.month) not in seen:
                seen.add((d.year, d.month))
                sig[i] = True
    elif t in ("days_before_earnings", "days_after_earnings"):
        n = int(entry.get("n") if entry.get("n") is not None else (1 if t == "days_before_earnings" else 0))
        off = -n if t == "days_before_earnings" else n
        for first, last in reactions:
            i = a.index_of(cal.add_trading_days(first if t == "days_before_earnings" else last, off))
            if i is not None:
                sig[i] = True
    elif t == "gap":
        x = float(entry.get("value") or 0.0)
        g = np.full(n_bars, np.nan)
        g[1:] = a.open[1:] / c[:-1] - 1.0
        with np.errstate(invalid="ignore"):
            sig = (g >= x) if entry.get("direction", "up") == "up" else (g <= -x)
    elif t == "rsi":
        v = float(entry["value"])
        r = rsi_series(c, int(entry.get("n") or 14))
        with np.errstate(invalid="ignore"):
            cond = (r < v) if entry.get("direction", "below") == "below" else (r > v)
        sig = _cross(cond & np.isfinite(r))
        sig[1:] &= np.isfinite(r[:-1])
    elif t == "sma_cross":
        s = sma_series(c, int(entry.get("n") or 50))
        with np.errstate(invalid="ignore"):
            cond = (c > s) if entry.get("direction", "above") == "above" else (c < s)
        ok = np.isfinite(s)
        sig = _cross(cond & ok)
        sig[1:] &= ok[:-1]
        sig[0] = False
    elif t == "new_52w":
        w = 252
        high = entry.get("direction", "high") == "high"
        for i in range(w, n_bars):
            prior = c[i - w:i]
            sig[i] = c[i] > prior.max() if high else c[i] < prior.min()
    else:
        raise ValueError(f"unknown entry type {t!r}")
    sig[:lo] = False
    return sig


def _simulate_symbol(
    sym: str, a: BarArrays, entry: Mapping, exit_: Mapping, side: str,
    reactions: list[tuple[date, date]], lo: int, hi: int,
) -> tuple[list[dict], list[tuple[int, int]]]:
    """Trades plus their (entry index, exit index) for one symbol over bars [lo, hi]."""
    sig = entry_signals(a, entry, reactions, lo)
    hold = exit_.get("hold_days")
    sl, tp = exit_.get("stop_loss"), exit_.get("take_profit")
    at_earn = bool(exit_.get("at_earnings_reaction"))
    if not hold and sl is None and tp is None and not at_earn:
        hold = 5
    react_idx = sorted(i for _, last in reactions if (i := a.index_of(last)) is not None)   # exit after both
    sign = 1.0 if side == "long" else -1.0
    trades, spans = [], []
    i = lo
    while i <= hi:
        if not sig[i]:
            i += 1
            continue
        e = float(a.close[i])
        next_react = next((k for k in react_idx if k > i), None) if at_earn else None
        j, note = hi, "open at end"
        for k in range(i + 1, hi + 1):
            ret_k = sign * (a.close[k] / e - 1.0)
            if sl is not None and ret_k <= -float(sl):
                j, note = k, "stop loss"
                break
            if tp is not None and ret_k >= float(tp):
                j, note = k, "take profit"
                break
            if next_react is not None and k == next_react:
                j, note = k, "earnings reaction"
                break
            if hold and k - i >= int(hold):
                j, note = k, f"hold {int(hold)}d"
                break
        if j == i:  # entry on the last bar: nothing to hold
            break
        x = float(a.close[j])
        trades.append({
            "symbol": sym, "entry_date": a.dates[i], "exit_date": a.dates[j],
            "legs": f"{side} {sym}", "entry_value": e, "exit_value": x,
            "pnl": sign * (x - e), "ret": sign * (x / e - 1.0), "pricing": "real", "note": note,
        })
        spans.append((i, j))
        i = j + 1
    return trades, spans


def backtest_stock(
    bars_by_symbol: Mapping[str, Sequence[Bar]], entry: Mapping, exit: Mapping,
    side: str = "long", start: date | None = None, end: date | None = None,
    earnings_by_symbol: Mapping[str, Sequence[EarningsEvent]] | None = None,
    initial: float = 10000.0,
) -> dict:
    """Rule backtest over one or more symbols → BacktestResult.

    Entries at the signal-day close (rules in `entry_signals`), one open position per symbol.
    Exit = first hit on daily closes of stop_loss / take_profit (fractions of entry), the close of
    the first earnings reaction day after entry, or hold_days; default hold_days=5 when no exit
    rule is given; positions still open at `end` exit at the last bar (note "open at end").
    Long ret = exit/entry − 1, short ret = 1 − exit/entry; pnl per share.

    Equity: daily from `initial`; each trade's value path V_t = 1 + ret_t (marked at closes);
    the portfolio's daily return is the mean of V_t/V_{t−1} − 1 over trades open that day (0 when
    flat) — exact compounding for a single symbol. Benchmark: equal-weight buy & hold of the
    symbols on the same dates and start value."""
    if side not in ("long", "short"):
        raise ValueError("side must be long|short")
    sign = 1.0 if side == "long" else -1.0
    all_trades: list[dict] = []
    daily: dict[date, list[float]] = {}
    norm_closes: dict[str, dict[date, float]] = {}
    eq_dates: set[date] = set()
    notes: list[str] = []
    for sym, bars in bars_by_symbol.items():
        a = bar_arrays(bars)
        if not len(a):
            notes.append(f"{sym}: no price history")
            continue
        lo = a.index_on_or_after(start) if start else 0
        hi = a.index_before(end + timedelta(days=1)) if end else len(a) - 1
        if lo is None or hi is None or hi < lo:
            notes.append(f"{sym}: no bars in range")
            continue
        reactions = _reactions((earnings_by_symbol or {}).get(sym, ()))
        trades, spans = _simulate_symbol(sym, a, entry, exit, side, reactions, lo, hi)
        all_trades.extend(trades)
        for (i, j) in spans:
            e = a.close[i]
            v = 1.0 + sign * (a.close[i:j + 1] / e - 1.0)
            for k in range(1, len(v)):
                daily.setdefault(a.dates[i + k], []).append(float(v[k] / v[k - 1] - 1.0))
        eq_dates.update(a.dates[lo:hi + 1])
        base = a.close[lo]
        norm_closes[sym] = {a.dates[k]: float(a.close[k] / base) for k in range(lo, hi + 1)}
    dates = sorted(eq_dates)
    equity, value = [], initial
    for d in dates:
        if d in daily:
            value *= 1.0 + float(np.mean(daily[d]))
        equity.append((d, value))
    benchmark = None
    if norm_closes:
        last = {s: 1.0 for s in norm_closes}
        benchmark = []
        for d in dates:
            for s, m in norm_closes.items():
                if d in m:
                    last[s] = m[d]
            benchmark.append((d, initial * float(np.mean(list(last.values())))))
    all_trades.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    notes.append(f"Equity starts at ${initial:,.0f}, fully invested in each open position (equal weights when "
                 "several symbols are open) and compounded; total P/L is the equity's dollar change. Trade P/L is "
                 "per share.")
    uses_earnings = entry["type"] in ("days_before_earnings", "days_after_earnings") or exit.get("at_earnings_reaction")
    if uses_earnings and any(e.timing == "unknown" for evs in (earnings_by_symbol or {}).values() for e in evs):
        notes.append(UNKNOWN_TIMING_NOTE)
    return build_result(all_trades, equity, benchmark, notes)
