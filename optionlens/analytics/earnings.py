"""Earnings analytics (SPEC §6 "Earnings move"): reaction day, per-event moves and drifts, stats,
implied earnings move and excess (event) implied move.

Pure functions over daily bars (ascending) and EarningsEvent records.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Mapping, Sequence

import numpy as np

from optionlens.analytics import calendar as cal
from optionlens.analytics.stats import BarArrays, bar_arrays
from optionlens.models import Bar, EarningsEvent

PRE_DRIFTS = (5, 10)
POST_DRIFTS = (1, 5, 10, 20)


def _gap(a: BarArrays, i: int) -> float | None:
    """open(i)/close(i−1) − 1, None if not computable."""
    if i is None or i < 1 or not math.isfinite(a.open[i]):
        return None
    return float(a.open[i] / a.close[i - 1] - 1.0)


def reaction_from_arrays(a: BarArrays, event_date: date, timing: str) -> tuple[date | None, bool]:
    """`reaction_date` over pre-built BarArrays (avoids rebuilding arrays per event)."""
    bmo_day = event_date if cal.is_trading_day(event_date) else cal.next_trading_day(event_date)
    amc_day = cal.next_trading_day(event_date)
    if timing == "bmo":
        return (bmo_day if a.index_of(bmo_day) is not None else None), False
    if timing == "amc":
        return (amc_day if a.index_of(amc_day) is not None else None), False
    # unknown: compare the gaps on the two candidate days
    if bmo_day == amc_day:  # event on a non-trading day: both readings give the same day
        return (amc_day if a.index_of(amc_day) is not None else None), True
    g_bmo = _gap(a, a.index_of(bmo_day))
    g_amc = _gap(a, a.index_of(amc_day))
    if g_bmo is None or g_amc is None:
        return None, True
    return (amc_day if abs(g_amc) > abs(g_bmo) else bmo_day), True


def reaction_date(event_date: date, timing: str, bars: Sequence[Bar]) -> tuple[date | None, bool]:
    """(reaction day, timing_inferred). bmo → the event day (rolled forward to a trading day);
    amc → next trading day; unknown → amc when |gap| on the next trading day > |gap| on the
    event day, else bmo (inferred=True). None when the needed bars are missing."""
    return reaction_from_arrays(bar_arrays(bars), event_date, timing)


def _ret(a: float, b: float) -> float | None:
    v = a / b - 1.0
    return float(v) if math.isfinite(v) else None


def _event_row(a: BarArrays, ev: EarningsEvent, implied: Mapping[date, float] | None,
               iv30: Mapping[date, float] | None) -> dict | None:
    react, inferred = reaction_from_arrays(a, ev.date, ev.timing)
    if react is None:
        return None
    r = a.index_of(react)
    p = r - 1
    row = {
        "date": ev.date, "timing": ev.timing, "timing_inferred": inferred, "reaction_date": react,
        "fiscal_quarter": ev.fiscal_quarter, "eps_estimate": ev.eps_estimate,
        "eps_actual": ev.eps_actual, "surprise_pct": ev.surprise_pct,
        "pre_close": None, "gap_pct": None, "move_pct": None, "max_move_pct": None,
        **{f"pre_drift_{n}": None for n in PRE_DRIFTS},
        **{f"post_drift_{n}": None for n in POST_DRIFTS},
        "implied_move_pct": (implied or {}).get(ev.date),
        "iv30_before": None, "iv30_after": (iv30 or {}).get(react),
    }
    if p < 0:
        return row
    pc = float(a.close[p])
    row["pre_close"] = pc
    row["iv30_before"] = (iv30 or {}).get(a.dates[p])
    if math.isfinite(a.open[r]):
        row["gap_pct"] = _ret(a.open[r], pc)
    row["move_pct"] = _ret(a.close[r], pc)
    ext = [abs(x / pc - 1.0) for x in (a.high[r], a.low[r]) if math.isfinite(x)]
    row["max_move_pct"] = float(max(ext)) if ext else None
    for n in PRE_DRIFTS:
        if p - n >= 0:
            row[f"pre_drift_{n}"] = _ret(pc, a.close[p - n])
    for n in POST_DRIFTS:
        if r + n < len(a):
            row[f"post_drift_{n}"] = _ret(a.close[r + n], a.close[r])
    return row


def event_rows(
    events: Sequence[EarningsEvent], bars: Sequence[Bar], *,
    implied_moves: Mapping[date, float] | None = None,
    iv30_by_date: Mapping[date, float] | None = None, limit: int = 40,
) -> list[dict]:
    """EarningsEventRow dicts, newest first, only events whose reaction-day bar exists.

    pre_close = close of the last bar before the reaction day; drifts are in bars (trading
    days); iv30_before = IV30 on the pre-close day, iv30_after = IV30 on the reaction day;
    implied_move_pct is looked up by event date."""
    a = bar_arrays(bars)
    rows = []
    for ev in sorted(events, key=lambda e: e.date, reverse=True):
        row = _event_row(a, ev, implied_moves, iv30_by_date)
        if row is not None:
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def _avg(xs: list[float]) -> float | None:
    return float(np.mean(xs)) if xs else None


def earnings_stats(rows: Sequence[Mapping]) -> dict:
    """EarningsStats over the given rows (pass rows[:12] for "last 12"). count = rows with a move."""
    mv = [r["move_pct"] for r in rows if r.get("move_pct") is not None]
    gaps = [abs(r["gap_pct"]) for r in rows if r.get("gap_pct") is not None]

    def col(k: str) -> list[float]:
        return [r[k] for r in rows if r.get(k) is not None]

    return {
        "count": len(mv),
        "avg_move": _avg(mv),
        "avg_abs_move": _avg([abs(x) for x in mv]),
        "median_abs_move": float(np.median(np.abs(mv))) if mv else None,
        "stdev_move": float(np.std(mv, ddof=1)) if len(mv) > 1 else None,
        "up_pct": _avg([1.0 if x > 0 else 0.0 for x in mv]),
        "max_up": float(max(mv)) if mv else None,
        "max_down": float(min(mv)) if mv else None,
        "avg_abs_gap": _avg(gaps),
        "avg_pre_drift_5": _avg(col("pre_drift_5")),
        "avg_post_drift_5": _avg(col("post_drift_5")),
        "avg_post_drift_20": _avg(col("post_drift_20")),
    }


def implied_earnings_move(expiries: Sequence[Mapping], reaction: date, spot: float) -> dict:
    """{implied_move_pct, excess_implied_move_pct, expiry_used}.

    implied = straddle of the first expiry ≥ reaction day / spot. excess (single-day event vol,
    term-structure method) = sqrt(max(0, σf²·tf − σb²·(tf − 1/252))) with σf/tf the ATM IV and
    year fraction of that expiry and σb the ATM IV of the next expiry (ex-event baseline)."""
    exps = sorted(expiries, key=lambda e: e["expiry"])
    k = next((i for i, e in enumerate(exps) if e["expiry"] >= reaction), None)
    out = {"implied_move_pct": None, "excess_implied_move_pct": None, "expiry_used": None}
    if k is None:
        return out
    f = exps[k]
    out["expiry_used"] = f["expiry"]
    if f.get("straddle") is not None and spot and spot > 0:
        out["implied_move_pct"] = float(f["straddle"]) / spot
    if k + 1 < len(exps):
        sf, sb, tf = f.get("atm_iv"), exps[k + 1].get("atm_iv"), f.get("t")
        if sf is not None and sb is not None and tf is not None and tf > 0:
            w = sf * sf * tf - sb * sb * max(tf - 1.0 / 252.0, 0.0)
            out["excess_implied_move_pct"] = math.sqrt(max(0.0, w))
    return out


def implied_vs_hist_ratio(implied: float | None, avg_abs_move: float | None) -> float | None:
    if implied is None or not avg_abs_move:
        return None
    return implied / avg_abs_move
