"""Dividend analytics (SPEC §6 "Dividend capture", §8 dividends tab, §7 dividend reports).

Regular dividends are those whose `kind` does not mention "special"/"extra"/"liquidat"/"return of
capital". Pure functions.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

import numpy as np

from optionlens.analytics.stats import bar_arrays
from optionlens.models import Bar, Dividend

_IRREGULAR = ("special", "extra", "liquidat", "return of capital")


def is_regular(d: Dividend) -> bool:
    k = (d.kind or "").lower()
    return not any(w in k for w in _IRREGULAR)


def _regular_sorted(divs: Sequence[Dividend], asof: date | None = None) -> list[Dividend]:
    return sorted((d for d in divs if is_regular(d) and d.amount and d.amount > 0
                   and (asof is None or d.ex_date <= asof)), key=lambda d: d.ex_date)


def frequency(divs: Sequence[Dividend], asof: date | None = None) -> int | None:
    """Payments per year (12/4/2/1) from the median spacing of the last ≤8 regular ex-dates."""
    reg = _regular_sorted(divs, asof)[-8:]
    if len(reg) < 2:
        return None
    gap = float(np.median([(b.ex_date - a.ex_date).days for a, b in zip(reg, reg[1:])]))
    if gap < 45:
        return 12
    if gap < 135:
        return 4
    if gap < 270:
        return 2
    return 1


def annual_rate(divs: Sequence[Dividend], asof: date) -> float | None:
    """Indicated annual rate = latest regular amount (ex-date ≤ asof) × payments per year."""
    reg = _regular_sorted(divs, asof)
    f = frequency(divs, asof)
    if not reg or f is None:
        return None
    return reg[-1].amount * f


def _ttm(divs: Sequence[Dividend], end: date, regular_only: bool) -> float:
    start = end - timedelta(days=365)
    return float(sum(d.amount for d in divs if start < d.ex_date <= end and d.amount
                     and (not regular_only or is_regular(d))))


def trailing_yield(divs: Sequence[Dividend], price: float | None, asof: date) -> float:
    """q for BSM: trailing-12-month cash dividends (all kinds, ex-date in (asof−365d, asof]) / price.
    0 when there are none or price is unknown."""
    if not price or price <= 0:
        return 0.0
    return _ttm(divs, asof, regular_only=False) / price


def growth(divs: Sequence[Dividend], asof: date, years: int) -> float | None:
    """CAGR of trailing-12m regular dividends: (TTM(asof)/TTM(asof − years))^(1/years) − 1.
    None unless both windows are non-zero and history reaches back to the earlier window."""
    reg = _regular_sorted(divs)
    if not reg:
        return None
    past_end = asof - timedelta(days=round(365.25 * years))
    if reg[0].ex_date > past_end:  # history does not reach the earlier window
        return None
    now_v, past_v = _ttm(reg, asof, True), _ttm(reg, past_end, True)
    if now_v <= 0 or past_v <= 0:
        return None
    return (now_v / past_v) ** (1.0 / years) - 1.0


def is_dividend_increase(divs: Sequence[Dividend], asof: date | None = None) -> bool:
    """Latest regular amount > the previous regular amount."""
    reg = _regular_sorted(divs, asof)
    return len(reg) >= 2 and reg[-1].amount > reg[-2].amount + 1e-12


def capture_rows(divs: Sequence[Dividend], bars: Sequence[Bar]) -> list[dict]:
    """Per ex-date (newest first): buy at the close before the ex-date, sell at the ex-date close.
    recovery_days = trading days (bars) from the ex-date until the first close ≥ pre_close
    (0 when the ex-date close already recovered; None if never)."""
    a = bar_arrays(bars)
    out = []
    for d in sorted(divs, key=lambda x: x.ex_date, reverse=True):
        i = a.index_of(d.ex_date)
        p = a.index_before(d.ex_date) if len(a) else None
        pre = float(a.close[p]) if p is not None else None
        ex = float(a.close[i]) if i is not None else None
        row = {"ex_date": d.ex_date, "amount": d.amount, "pre_close": pre, "ex_close": ex,
               "price_return": None, "total_return": None, "recovery_days": None}
        if pre and ex is not None:
            row["price_return"] = ex / pre - 1.0
            row["total_return"] = (ex + d.amount) / pre - 1.0
            hit = np.nonzero(a.close[i:] >= pre)[0]
            row["recovery_days"] = int(hit[0]) if len(hit) else None
        out.append(row)
    return out


def capture_stats(rows: Sequence[dict]) -> dict:
    tr = [r["total_return"] for r in rows if r["total_return"] is not None]
    rec = [r["recovery_days"] for r in rows if r["recovery_days"] is not None]
    return {
        "count": len(tr),
        "avg_total_return": float(np.mean(tr)) if tr else None,
        "win_rate": float(np.mean([t > 0 for t in tr])) if tr else None,
        "avg_recovery_days": float(np.mean(rec)) if rec else None,
    }


def next_dividend(divs: Sequence[Dividend], price: float | None, asof: date) -> dict | None:
    """NextDividend for the first ex-date ≥ asof; yield = indicated annual rate / price."""
    fut = sorted((d for d in divs if d.ex_date >= asof), key=lambda d: d.ex_date)
    if not fut:
        return None
    d = fut[0]
    rate = annual_rate(divs, asof)
    y = rate / price if rate and price and price > 0 else None
    return {"ex_date": d.ex_date, "amount": d.amount, "days": (d.ex_date - asof).days, "yield": y}


def dividend_summary(symbol: str, divs: Sequence[Dividend], bars: Sequence[Bar],
                     price: float | None, asof: date) -> dict:
    """Dividends shape. history/capture newest first."""
    rate = annual_rate(divs, asof)
    cap = capture_rows([d for d in divs if d.ex_date <= asof], bars)
    return {
        "symbol": symbol,
        "annual_rate": rate,
        "yield": rate / price if rate is not None and price and price > 0 else None,
        "frequency": frequency(divs, asof),
        "growth_1y": growth(divs, asof, 1),
        "growth_3y": growth(divs, asof, 3),
        "growth_5y": growth(divs, asof, 5),
        "next": next_dividend(divs, price, asof),
        "history": [{"ex_date": d.ex_date, "amount": d.amount, "pay_date": d.pay_date,
                     "record_date": d.record_date, "declared_date": d.declared_date, "kind": d.kind}
                    for d in sorted(divs, key=lambda x: x.ex_date, reverse=True)],
        "capture": cap,
        "capture_stats": capture_stats(cap),
    }
