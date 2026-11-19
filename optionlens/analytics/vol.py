"""Realized and implied volatility analytics (SPEC §6). Pure functions."""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from .chain import ChainArrays

HV_WINDOWS = (10, 20, 30, 60, 90, 252)
CM_TENORS = (30, 60, 90, 120, 180, 360)
ANNUAL = math.sqrt(252.0)
MIN_IV_HISTORY = 20


# ----------------------------------------------------------------------------- realized vol
def log_returns(closes) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    return np.diff(np.log(c))


def hv(closes, n: int) -> float | None:
    """HV_n = stdev(last n log returns, ddof=1)·√252. None without n+1 finite closes."""
    c = np.asarray(closes, dtype=float)
    if n < 2 or len(c) < n + 1:
        return None
    r = log_returns(c[-(n + 1):])
    if not np.all(np.isfinite(r)):
        return None
    return float(np.std(r, ddof=1) * ANNUAL)


def hv_series(closes, n: int) -> np.ndarray:
    """Rolling HV_n aligned to closes: value at i uses the n log returns ending at close i (NaN
    for i < n)."""
    c = np.asarray(closes, dtype=float)
    out = np.full(len(c), np.nan)
    if n < 2 or len(c) < n + 1:
        return out
    r = log_returns(c)
    win = np.lib.stride_tricks.sliding_window_view(r, n)
    out[n:] = np.std(win, axis=1, ddof=1) * ANNUAL
    return out


def hv_snapshot(closes) -> dict:
    """{hv10, hv20, hv30, hv60, hv90, hv252} from closes (ascending)."""
    return {f"hv{n}": hv(closes, n) for n in HV_WINDOWS}


def parkinson(high, low, n: int = 20) -> float | None:
    """sqrt(Σ ln(H/L)² / (4 n ln2))·√252 over the last n bars."""
    h, l = np.asarray(high, dtype=float), np.asarray(low, dtype=float)
    if len(h) < n or n < 1:
        return None
    hl = np.log(h[-n:] / l[-n:])
    if not np.all(np.isfinite(hl)):
        return None
    return float(math.sqrt(np.sum(hl * hl) / (4.0 * n * math.log(2.0))) * ANNUAL)


def realized_forward(closes, window: int = 21) -> np.ndarray:
    """Realized vol over the NEXT `window` trading days, aligned to closes: value at i uses log
    returns of closes i..i+window (NaN where the future is not yet known)."""
    c = np.asarray(closes, dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) < window + 1:
        return out
    r = log_returns(c)
    win = np.lib.stride_tricks.sliding_window_view(r, window)
    out[: len(win)] = np.std(win, axis=1, ddof=1) * ANNUAL
    return out


# ----------------------------------------------------------------------------- IV rank
def iv_rank_percentile(history: Sequence[float | None], current: float | None = None) -> dict:
    """IV rank/percentile over the last 252 stored values incl. today (SPEC §6).

    ``history`` is chronological stored iv30 values ending with today's (None/NaN are dropped);
    ``current`` defaults to the last value. Returns {iv_rank, iv_percentile, history_days,
    iv_high_52w, iv_low_52w}; rank/percentile None with fewer than 20 values. Flat history ->
    rank None (0/0), percentile 0.
    """
    vals = np.array([v for v in history if v is not None], dtype=float)
    vals = vals[np.isfinite(vals)][-252:]
    out = {"iv_rank": None, "iv_percentile": None, "history_days": int(len(vals)),
           "iv_high_52w": float(vals.max()) if len(vals) else None,
           "iv_low_52w": float(vals.min()) if len(vals) else None}
    cur = current if current is not None else (float(vals[-1]) if len(vals) else None)
    if cur is None or len(vals) < MIN_IV_HISTORY:
        return out
    lo, hi = vals.min(), vals.max()
    out["iv_rank"] = float((cur - lo) / (hi - lo)) if hi > lo else None
    out["iv_percentile"] = float(np.mean(vals < cur))
    return out


# ----------------------------------------------------------------------------- implied vol
def atm_iv_by_expiry(ca: ChainArrays) -> list[dict]:
    """Per expiry {expiry, dte, t, atm_iv}: at each strike average call & put IV (whichever is
    known), take the nearest strikes with IV at/below and at/above spot, interpolate linearly in
    strike to spot (one-sided -> that strike's IV). Vectorized across expiries."""
    atm = atm_iv_array(ca)
    t, dte = ca.exp_t, ca.exp_dte
    return [{"expiry": e, "dte": int(dte[i]), "t": float(t[i]),
             "atm_iv": None if np.isnan(atm[i]) else float(atm[i])} for i, e in enumerate(ca.expiries)]


def atm_iv_array(ca: ChainArrays) -> np.ndarray:
    """ATM IV per unique expiry (NaN when no strike has an IV)."""
    ne = len(ca.expiries)
    out = np.full(ne, np.nan)
    ok = np.isfinite(ca.iv)
    if not ok.any():
        return out
    big = 1e7
    key = ca.exp_idx[ok] * big + ca.strike[ok]
    gk, inv = np.unique(key, return_inverse=True)
    avg = np.bincount(inv, weights=ca.iv[ok]) / np.bincount(inv)
    gexp = np.floor(gk / big + 1e-9).astype(int)
    gstrike = gk - gexp * big
    target = np.arange(ne) * big + ca.spot
    hi = np.searchsorted(gk, target, side="left")          # first strike >= spot
    lo = np.searchsorted(gk, target, side="right") - 1      # last strike <= spot
    e = np.arange(ne)
    lo_ok = (lo >= 0) & (gexp[np.clip(lo, 0, None)] == e)
    hi_ok = (hi < len(gk)) & (gexp[np.clip(hi, None, len(gk) - 1)] == e)
    loc, hic = np.clip(lo, 0, len(gk) - 1), np.clip(hi, 0, len(gk) - 1)
    k0, k1 = gstrike[loc], gstrike[hic]
    v0, v1 = avg[loc], avg[hic]
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(k1 > k0, (ca.spot - k0) / (k1 - k0), 0.0)
    both = lo_ok & hi_ok
    out[both] = (v0 + w * (v1 - v0))[both]
    out[lo_ok & ~hi_ok] = v0[lo_ok & ~hi_ok]
    out[hi_ok & ~lo_ok] = v1[hi_ok & ~lo_ok]
    return out


def _term_points(rows: Sequence[dict], key: str) -> tuple[np.ndarray, np.ndarray]:
    pts = [(r["t"], r[key]) for r in rows
           if r.get(key) is not None and r.get("dte") is not None and r["dte"] >= 1
           and np.isfinite(r[key]) and r[key] > 0]
    pts.sort()
    return np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float)


def interp_total_variance(t: np.ndarray, iv: np.ndarray, target_t: float) -> float | None:
    """Linear interpolation in total variance w = σ²t between bracketing points; flat (in σ)
    extrapolation outside. ``t`` ascending."""
    if len(t) == 0:
        return None
    if target_t <= t[0]:
        return float(iv[0])
    if target_t >= t[-1]:
        return float(iv[-1])
    j = int(np.searchsorted(t, target_t))
    t0, t1 = t[j - 1], t[j]
    w0, w1 = iv[j - 1] ** 2 * t0, iv[j] ** 2 * t1
    w = w0 + (w1 - w0) * (target_t - t0) / (t1 - t0)
    return float(math.sqrt(w / target_t)) if w > 0 else None


def constant_maturity(term_rows: Sequence[dict], tenors: Sequence[int] = CM_TENORS,
                      key: str = "atm_iv") -> dict[int, float | None]:
    """{tenor_days: IV} from per-expiry rows ({t, dte, <key>}), ignoring expiries with DTE < 1."""
    t, iv = _term_points(term_rows, key)
    return {d: interp_total_variance(t, iv, d / 365.0) for d in tenors}


def forward_vol(iv1: float, t1: float, iv2: float, t2: float) -> float | None:
    """sqrt((σ2²t2 − σ1²t1)/(t2 − t1)); None if t2 <= t1 or the forward variance is negative."""
    if None in (iv1, iv2, t1, t2) or t2 <= t1:
        return None
    fv = (iv2 * iv2 * t2 - iv1 * iv1 * t1) / (t2 - t1)
    return float(math.sqrt(fv)) if fv >= 0 else None


def _iv_at_delta(d: np.ndarray, iv: np.ndarray, target: float) -> float | None:
    ok = np.isfinite(d) & np.isfinite(iv)
    if ok.sum() < 2:
        return None
    d, iv = d[ok], iv[ok]
    order = np.argsort(d, kind="stable")
    d, iv = d[order], iv[order]
    if not (d[0] <= target <= d[-1]):
        return None
    return float(np.interp(target, d, iv))


def delta_skew_by_expiry(ca: ChainArrays, target: float = 0.25) -> list[dict]:
    """Per expiry {expiry, dte, t, call25, put25}: IV interpolated linearly in (feed) delta at
    call delta +target and put delta −target (None when not bracketed)."""
    out = []
    for i, sl in enumerate(ca.expiry_slices()):
        c = ca.is_call[sl]
        d, iv = ca.delta[sl], ca.iv[sl]
        out.append({
            "expiry": ca.expiries[i], "dte": int(ca.dte[sl][0]), "t": float(ca.t[sl][0]),
            "call25": _iv_at_delta(d[c], iv[c], target),
            "put25": _iv_at_delta(d[~c], iv[~c], -target),
        })
    return out


def skew_30(ca: ChainArrays, atm30: float | None) -> dict:
    """{skew25_30, put_skew_30, put25_30, call25_30} (SPEC §6)."""
    rows = delta_skew_by_expiry(ca)
    p = constant_maturity(rows, (30,), key="put25")[30]
    c = constant_maturity(rows, (30,), key="call25")[30]
    return {
        "put25_30": p, "call25_30": c,
        "skew25_30": p - c if p is not None and c is not None else None,
        "put_skew_30": p - atm30 if p is not None and atm30 is not None else None,
    }
