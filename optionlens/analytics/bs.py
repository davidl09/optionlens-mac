"""Black-Scholes-Merton with continuous dividend yield (SPEC §6).

Conventions: T in years (calendar days / 365), r and q continuous, sigma a fraction.
Greeks: delta per $1, gamma per $1, vega per 1 vol point (/100), theta per calendar day (/365),
rho per 1% rate move (/100). All functions broadcast over numpy arrays; ``right`` may be "C"/"P",
a bool (True = call) or an array of either.

Edge cases: T <= 0 -> intrinsic value (greeks: delta 0/±1, others 0); sigma <= 0 -> discounted
intrinsic of the forward.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import brentq
from scipy.special import ndtr

IV_LOW, IV_HIGH = 1e-4, 5.0
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def is_call(right) -> np.ndarray | bool:
    """Normalize a right spec ("C"/"P", bool, or arrays thereof) to bool (True = call)."""
    if isinstance(right, str):
        return right.upper() == "C"
    arr = np.asarray(right)
    if arr.dtype.kind in "USO":
        return np.char.upper(arr.astype(str)) == "C"
    return arr.astype(bool)


def _pdf(x):
    return np.exp(-0.5 * x * x) / _SQRT_2PI


def _prep(S, K, T, r, q, sigma):
    S, K, T, r, q, sigma = (np.asarray(a, dtype=float) for a in (S, K, T, r, q, sigma))
    live = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    Ts = np.where(live, T, 1.0)
    ss = np.where(live, sigma, 1.0)
    Ss = np.where(live, S, 1.0)
    Ks = np.where(live, K, 1.0)
    sqrtT = np.sqrt(Ts)
    d1 = (np.log(Ss / Ks) + (r - q + 0.5 * ss * ss) * Ts) / (ss * sqrtT)
    d2 = d1 - ss * sqrtT
    return S, K, T, r, q, sigma, live, d1, d2, sqrtT


def _out(x):
    return float(x) if np.ndim(x) == 0 else x


def price(S, K, T, r, q, sigma, right):
    """BSM option price."""
    c = is_call(right)
    S, K, T, r, q, sigma, live, d1, d2, _ = _prep(S, K, T, r, q, sigma)
    Tp = np.maximum(T, 0.0)
    dfq, dfr = np.exp(-q * Tp), np.exp(-r * Tp)
    call = S * dfq * ndtr(d1) - K * dfr * ndtr(d2)
    put = K * dfr * ndtr(-d2) - S * dfq * ndtr(-d1)
    val = np.where(c, call, put)
    # degenerate: expired -> intrinsic; zero vol -> discounted forward intrinsic
    fwd_intr = np.where(c, np.maximum(S * dfq - K * dfr, 0.0), np.maximum(K * dfr - S * dfq, 0.0))
    spot_intr = np.where(c, np.maximum(S - K, 0.0), np.maximum(K - S, 0.0))
    val = np.where(live, val, np.where(T > 0, fwd_intr, spot_intr))
    return _out(val)


def greeks(S, K, T, r, q, sigma, right) -> dict:
    """dict(delta, gamma, vega, theta, rho) in the module's unit conventions."""
    c = is_call(right)
    S, K, T, r, q, sigma, live, d1, d2, sqrtT = _prep(S, K, T, r, q, sigma)
    Tp = np.where(live, T, 1.0)
    dfq, dfr = np.exp(-q * Tp), np.exp(-r * Tp)
    nd1 = _pdf(d1)
    Ss = np.where(live, S, 1.0)
    ss = np.where(live, sigma, 1.0)
    delta = np.where(c, dfq * ndtr(d1), dfq * (ndtr(d1) - 1.0))
    gamma = dfq * nd1 / (Ss * ss * sqrtT)
    vega = Ss * dfq * nd1 * sqrtT / 100.0
    common = -Ss * dfq * nd1 * ss / (2.0 * sqrtT)
    theta_c = common - r * K * dfr * ndtr(d2) + q * Ss * dfq * ndtr(d1)
    theta_p = common + r * K * dfr * ndtr(-d2) - q * Ss * dfq * ndtr(-d1)
    theta = np.where(c, theta_c, theta_p) / 365.0
    rho = np.where(c, K * Tp * dfr * ndtr(d2), -K * Tp * dfr * ndtr(-d2)) / 100.0
    # degenerate cases: delta = in-the-money indicator, the rest 0
    itm = np.where(c, S > K, S < K)
    dead_delta = np.where(itm, np.where(c, 1.0, -1.0), 0.0)
    out = {
        "delta": np.where(live, delta, dead_delta),
        "gamma": np.where(live, gamma, 0.0),
        "vega": np.where(live, vega, 0.0),
        "theta": np.where(live, theta, 0.0),
        "rho": np.where(live, rho, 0.0),
    }
    return {k: _out(v) for k, v in out.items()}


def delta(S, K, T, r, q, sigma, right):
    return greeks(S, K, T, r, q, sigma, right)["delta"]


def no_arb_bounds(S: float, K: float, T: float, r: float, q: float, right) -> tuple[float, float]:
    """(lower, upper) European no-arbitrage price bounds."""
    dfq, dfr = math.exp(-q * T), math.exp(-r * T)
    if is_call(right):
        return max(0.0, S * dfq - K * dfr), S * dfq
    return max(0.0, K * dfr - S * dfq), K * dfr


def implied_vol(option_price: float, S: float, K: float, T: float, r: float, q: float, right,
                lo: float = IV_LOW, hi: float = IV_HIGH) -> float | None:
    """Brent solve on [lo, hi]. None if inputs are invalid, the price violates no-arb bounds, or
    the price is not attainable for any vol in [lo, hi]."""
    if not all(map(math.isfinite, (option_price, S, K, T, r, q))):
        return None
    if option_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    lower, upper = no_arb_bounds(S, K, T, r, q, right)
    if not (lower < option_price < upper):
        return None
    f = lambda s: price(S, K, T, r, q, s, right) - option_price  # noqa: E731
    flo, fhi = f(lo), f(hi)
    if flo > 0 or fhi < 0:
        return None
    if flo == 0:
        return lo
    if fhi == 0:
        return hi
    return float(brentq(f, lo, hi, xtol=1e-10, rtol=1e-12, maxiter=200))
