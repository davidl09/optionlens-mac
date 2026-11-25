"""Strategy calculator engine behind POST /api/calc/strategy (CalcResult, SPEC §8 calculator).

The caller resolves each Leg's price (default mid) and IV (default contract IV). Curves are P/L in
dollars (value − entry cost). The expiry curve is taken at the nearest option expiry: legs expiring
then are worth intrinsic; later legs are valued with BSM at 16:00 ET that day using their own IV
(+ iv_shift). PoP / EV integrate that same curve over the lognormal grid (2001 points, ±6σ):
IV measure = ``dist_iv`` (default: mean IV of the legs expiring at expiry_used, + iv_shift) with
drift r − q; HV measure = hv30 with drift 0.

``vol`` (strategies.LegVol) is the IV model: default each leg's own IV at every date; event_vol.CrushVol models the
earnings IV crush. ``reaction``: the next earnings reaction session — when it falls in (now, expiry_used], EV(HV30)
is not computed (HV30 holds no earnings move) and ev_hv_reason says so.
Per leg: iv_eval / iv_expiry = the volatility the leg is valued with at the evaluation date / at expiry_used (incl.
iv_shift; None when expired then or stock) and the leg's position greeks. vega_by_expiry: Σ leg vega per expiry
(sums to greeks.vega).
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Sequence

import numpy as np

from . import probability as prob
from .calendar import NY, year_fraction_to_expiry
from .event_vol import event_ahead
from .strategies import GREEKS, Leg, LegVol, close_dt, leg_greeks, leg_sigma, net_cost, value_at

SLOPE_EPS = 1e-6       # $ per $ of underlying: below this the far-right curve is flat
TABLE_ROWS = 15


def _pnl_fn(legs: Sequence[Leg], at: datetime, r: float, q: float, shift: float, cost: float, vol: LegVol | None):
    return lambda S: value_at(legs, S, at, r, q, shift, vol) - cost


def _sigma_at(l: Leg, at: datetime | None, shift: float, vol: LegVol | None) -> float | None:
    if l.kind != "option" or l.iv is None or at is None or close_dt(l.expiry) <= at:
        return None
    return float(leg_sigma(l, at, shift, vol))


def _breakevens(x: np.ndarray, y: np.ndarray) -> list[float]:
    out = []
    pos = y > 0
    for i in np.nonzero(pos[:-1] != pos[1:])[0]:
        a, b = y[i], y[i + 1]
        out.append(float(x[i] + (x[i + 1] - x[i]) * a / (a - b)))
    return out


def calculate(legs: Sequence[Leg], spot: float, now: datetime, r: float, q: float, *,
              hv30: float | None = None, iv_shift: float = 0.0, eval_date: date | None = None,
              price_range: tuple[float, float] | None = None, points: int = 121,
              dist_iv: float | None = None, asof: datetime | None = None, vol: LegVol | None = None,
              reaction: date | None = None) -> dict:
    """CalcResult (types.ts). ``legs`` must carry price; option legs need iv unless expired."""
    legs = list(legs)
    cost = net_cost(legs)
    opts = [l for l in legs if l.kind == "option"]
    first_exp = min((l.expiry for l in opts), default=None)
    exp_at = close_dt(first_exp) if first_exp else None

    # default range: spot ± max(20%, 3σ√T to the last expiry)
    if price_range is None:
        ivs = [l.iv + iv_shift for l in opts if l.iv is not None]
        sig = float(np.mean(ivs)) if ivs else (hv30 or 0.0)
        last = max((l.expiry for l in opts), default=None)
        T = year_fraction_to_expiry(last, now) if last else 0.0
        half = max(0.20, 3.0 * sig * math.sqrt(T))
        price_range = (max(spot * (1.0 - half), spot * 0.01), spot * (1.0 + half))
    lo, hi = price_range
    prices = np.linspace(lo, hi, max(int(points), 2))

    eval_at = now if eval_date is None or eval_date <= now.astimezone(NY).date() else close_dt(eval_date)
    pnl_today = _pnl_fn(legs, now, r, q, iv_shift, cost, vol)
    pnl_eval = _pnl_fn(legs, eval_at, r, q, iv_shift, cost, vol)
    pnl_exp = _pnl_fn(legs, exp_at, r, q, iv_shift, cost, vol) if exp_at else pnl_today

    # extrema & breakevens on a dense evaluation set incl. all strikes and the far ends
    strikes = np.array(sorted({l.strike for l in opts}), dtype=float)
    dense = np.unique(np.concatenate([np.linspace(lo, hi, 2001), strikes[(strikes >= lo) & (strikes <= hi)]]))
    y_dense = pnl_exp(dense)
    far = max(hi, spot, strikes.max() if len(strikes) else 0.0) * 5.0
    ext = np.unique(np.concatenate([[0.0], dense, strikes, [far]]))
    y_ext = pnl_exp(ext)
    slope = (pnl_exp(np.array([far * 1.1]))[0] - pnl_exp(np.array([far]))[0]) / (0.1 * far)
    max_profit = None if slope > SLOPE_EPS else float(y_ext.max())
    max_loss = None if slope < -SLOPE_EPS else float(-y_ext.min())

    pop = ev_iv = ev_hv = ev_hv_reason = None
    if exp_at is not None:
        T = year_fraction_to_expiry(first_exp, now)
        if dist_iv is None:
            front_iv = [l.iv for l in opts if l.expiry == first_exp and l.iv is not None]
            dist_iv = float(np.mean(front_iv)) if front_iv else None
        if dist_iv is not None:
            sig = max(dist_iv + iv_shift, 1e-4)
            z, P = prob.grid(spot, T, sig, r - q)
            pop_a, ev_a = prob.pop_ev_on_grid(z, pnl_exp(P))
            pop, ev_iv = float(pop_a), float(ev_a)
        if event_ahead(reaction, now, first_exp):
            ev_hv_reason = (f"Earnings react {reaction.isoformat()}, before {first_exp.isoformat()}: "
                            "HV30 holds no earnings move")
        elif hv30 is None:
            ev_hv_reason = "No HV30: needs 30 days of price history"
        else:
            z, P = prob.grid(spot, T, hv30, 0.0)
            ev_hv = float(prob.pop_ev_on_grid(z, pnl_exp(P))[1])

    y_exp, y_eval, y_today = pnl_exp(prices), pnl_eval(prices), pnl_today(prices)
    tprices = np.linspace(lo, hi, TABLE_ROWS)
    table = [{"price": float(p), "change_pct": float(p / spot - 1.0), "pnl_expiry": float(a), "pnl_eval": float(b)}
             for p, a, b in zip(tprices, pnl_exp(tprices), pnl_eval(tprices))]
    greeks = leg_greeks(legs, spot, now, r, q, iv_shift, vol)
    out_legs, by_exp = [], {}
    for l, g in zip(legs, greeks):
        d = l.as_dict()
        d["price"] = float(l.price or 0.0)
        d["iv_eval"] = _sigma_at(l, eval_at, iv_shift, vol)
        d["iv_expiry"] = _sigma_at(l, exp_at, iv_shift, vol)
        d["greeks"] = g
        if l.kind == "option":
            by_exp[l.expiry] = by_exp.get(l.expiry, 0.0) + g["vega"]
        out_legs.append(d)
    return {
        "spot": float(spot), "asof": asof, "legs": out_legs, "cost": cost,
        "prices": prices.tolist(), "pnl_expiry": y_exp.tolist(), "pnl_eval": y_eval.tolist(),
        "pnl_today": y_today.tolist(), "breakevens": _breakevens(dense, y_dense),
        "max_profit": max_profit, "max_loss": max_loss, "pop": pop, "ev_iv": ev_iv, "ev_hv": ev_hv,
        "ev_hv_reason": ev_hv_reason, "greeks": {k: float(sum(g[k] for g in greeks)) for k in GREEKS},
        "vega_by_expiry": [{"expiry": e, "vega": v} for e, v in sorted(by_exp.items())], "table": table,
        "expiry_used": first_exp,
    }
