"""Lognormal terminal-price engine (SPEC §6 "Probability").

ln S_T = ln S + (mu − σ²/2)·T + σ√T·Z. ``mu`` is r − q for the risk-neutral (IV) measure and 0
for the HV measure. Closed forms are used where they exist (P(S_T > K), E[(S_T − K)^+]); the grid
(2001 log-price points spanning ±6σ) handles arbitrary payoffs. On the grid, probability of profit
is computed by locating the payoff's sign changes (linear interpolation in z) and summing normal
CDF mass over the profitable intervals, so a payoff with a single breakeven reproduces the closed
form to ~1e-6. Expected values are undiscounted expected P/L at T.
All functions broadcast over numpy arrays.
"""
from __future__ import annotations


import numpy as np
from scipy.special import ndtr

GRID_POINTS = 2001
GRID_WIDTH = 6.0


def _d2(S, K, T, sigma, mu):
    S, K, T, sigma, mu = (np.asarray(a, dtype=float) for a in (S, K, T, sigma, mu))
    sd = sigma * np.sqrt(np.maximum(T, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        d2 = (np.log(S / K) + (mu - 0.5 * sigma * sigma) * T) / sd
    return d2, sd, S, K, T, sigma, mu


def prob_above(S, K, T, sigma, mu=0.0):
    """P(S_T > K) = N(d2). K <= 0 -> 1; degenerate vol/time -> indicator of the forward."""
    d2, sd, S, K, T, sigma, mu = _d2(S, K, T, sigma, mu)
    fwd = S * np.exp(mu * np.maximum(T, 0.0))
    p = np.where(sd > 0, ndtr(np.nan_to_num(d2, nan=0.0, posinf=40.0, neginf=-40.0)),
                 (fwd > K).astype(float))
    p = np.where(K <= 0, 1.0, p)
    p = np.where(np.isinf(K) & (K > 0), 0.0, p)
    return float(p) if np.ndim(p) == 0 else p


def expected_call(S, K, T, sigma, mu=0.0):
    """E[(S_T − K)^+] (undiscounted)."""
    d2, sd, S, K, T, sigma, mu = _d2(S, K, T, sigma, mu)
    fwd = S * np.exp(mu * np.maximum(T, 0.0))
    safe = sd > 0
    d2s = np.where(safe, np.nan_to_num(d2, nan=0.0, posinf=40.0, neginf=-40.0), 0.0)
    d1 = d2s + np.where(safe, sd, 0.0)
    val = np.where(safe, fwd * ndtr(d1) - K * ndtr(d2s), np.maximum(fwd - K, 0.0))
    val = np.where(K <= 0, fwd - K, val)
    return float(val) if np.ndim(val) == 0 else val


def expected_put(S, K, T, sigma, mu=0.0):
    """E[(K − S_T)^+] via parity: E[call] − (E[S_T] − K)."""
    S, K, T, mu = (np.asarray(a, dtype=float) for a in (S, K, T, mu))
    val = expected_call(S, K, T, sigma, mu) - (S * np.exp(mu * np.maximum(T, 0.0)) - K)
    val = np.maximum(val, 0.0)
    return float(val) if np.ndim(val) == 0 else val


def expected_spot(S, T, mu=0.0):
    return np.asarray(S, dtype=float) * np.exp(np.asarray(mu, dtype=float) * np.maximum(T, 0.0))


# ----------------------------------------------------------------------------- grid engine
def grid(S, T, sigma, mu=0.0, n: int = GRID_POINTS, width: float = GRID_WIDTH):
    """(z, prices): z shape (n,), prices shape broadcast(S,T,sigma,mu) + (n,)."""
    z = np.linspace(-width, width, n)
    S, T, sigma, mu = (np.asarray(a, dtype=float)[..., None] for a in (S, T, sigma, mu))
    prices = S * np.exp((mu - 0.5 * sigma * sigma) * T + sigma * np.sqrt(T) * z)
    return z, prices


def pop_ev_on_grid(z: np.ndarray, payoff: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Probability that payoff > 0 and E[payoff] for payoff values sampled on ``z`` (last axis).

    PoP sums N(z) mass over the profitable parts of each grid cell, with the crossing located by
    linear interpolation of the payoff in z; the tails beyond ±width take the sign of the end
    points. EV is the trapezoid integral against the normal density (renormalized over the grid).
    """
    p = np.asarray(payoff, dtype=float)
    z0, z1 = z[:-1], z[1:]
    a, b = p[..., :-1], p[..., 1:]
    pos_a, pos_b = a > 0, b > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        zc = z0 + (z1 - z0) * a / (a - b)
    zc = np.where(np.isfinite(zc), np.clip(zc, z0, z1), z0)
    Fz0, Fz1, Fzc = ndtr(z0), ndtr(z1), ndtr(zc)
    mass = np.where(pos_a & pos_b, Fz1 - Fz0,
                    np.where(pos_a & ~pos_b, Fzc - Fz0,
                             np.where(~pos_a & pos_b, Fz1 - Fzc, 0.0)))
    pop = mass.sum(axis=-1) + np.where(p[..., 0] > 0, ndtr(z[0]), 0.0) + np.where(p[..., -1] > 0, 1 - ndtr(z[-1]), 0.0)
    w = np.exp(-0.5 * z * z)
    w[0] *= 0.5
    w[-1] *= 0.5
    w /= w.sum()
    ev = (p * w).sum(axis=-1)
    return pop, ev
