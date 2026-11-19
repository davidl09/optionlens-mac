"""Option strategies: the single leg representation, strategy templates, payoff/valuation, and the
vectorized strategy screeners (SPEC §6, §7, §8 calculator/ideas, §9 backtests).

Units
-----
* ``Leg.qty``: contracts for options (×100 shares), shares for stock (API ``Leg`` contract).
* Position $ values = Σ qty · multiplier · price  (multiplier 100 for options, 1 for stock).
* Screener rows are for ONE unit of the strategy: 1 contract per option leg (2 for a fly body) and
  100 shares for stock legs. Per-share prices (``price*``) are net debit (+) / credit (−);
  ``cost``, ``max_profit``, ``max_loss``, ``ev_*`` are dollars per unit. ``max_loss`` is the
  magnitude of the worst P/L (positive = a loss); ``None`` means unlimited.

Screener row keys (every strategy)
----------------------------------
symbol, strategy, legs (text), expiry, back_expiry (calendars, else None), dte, spot, strikes
(list, leg order), price (per share, chosen pricing), price_natural, price_mid, pricing, cost,
is_credit, max_profit, max_loss, breakevens (list), breakeven_low, breakeven_high,
return_on_risk (max_profit/max_loss), annualized_return (return_on_risk·365/dte), pop,
prob_max_profit, ev_iv, ev_hv, delta (share-equivalent), gamma, theta ($/day), vega ($/vol pt),
iv (mean leg IV), atm_iv, iv_rank, hv30, earnings_before_expiry, next_earnings, min_leg_oi,
min_leg_volume, max_leg_spread_pct.
Extra keys: covered-calls: static_return, static_annualized, if_called_return,
if_called_annualized, downside_protection, prob_called. naked-puts: return_on_cash,
return_on_cash_annualized, margin_req, return_on_margin, return_on_margin_annualized, prob_otm.
calendar-*: front_iv, back_iv, iv_ratio, forward_vol, theta_ratio, est_max_profit.
synthetic-long: synthetic_price_natural, synthetic_price_mid, parity_gap_natural, parity_gap_mid.

Probability conventions: the IV distribution uses the expiry's ATM IV (vol.atm_iv_by_expiry) with
drift r − q; the HV distribution uses hv30 with drift 0 (SPEC §6). Expected values are undiscounted
expected P/L at expiry. Single-expiry payoffs are piecewise linear, so PoP / prob of max profit /
EV are computed exactly from the kinks (closed-form lognormal masses); calendars use the grid.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Callable, Iterable, Literal, Sequence

import numpy as np

from . import bs, probability as prob, vol
from .calendar import NY, REGULAR_CLOSE, year_fraction_to_expiry
from .chain import ChainArrays

Right = Literal["C", "P"]
OPTION_MULT = 100.0
UNIT_SHARES = 100  # shares of stock per strategy unit


# ============================================================================ legs / positions
@dataclass(frozen=True)
class Leg:
    kind: Literal["stock", "option"]
    qty: float                       # +long / −short; contracts for options, shares for stock
    right: Right | None = None
    strike: float | None = None
    expiry: date | None = None
    price: float | None = None       # entry price per share
    iv: float | None = None
    mid: float | None = None         # informational (calculator echo)

    @property
    def multiplier(self) -> float:
        return OPTION_MULT if self.kind == "option" else 1.0

    def as_dict(self) -> dict:
        return {"kind": self.kind, "right": self.right, "strike": self.strike, "expiry": self.expiry,
                "qty": self.qty, "price": self.price, "iv": self.iv, "mid": self.mid}


def _fmt_num(x: float) -> str:
    return f"{x:g}" if abs(x - round(x, 4)) < 1e-9 else f"{x:.4f}"


def describe_legs(legs: Sequence[Leg]) -> str:
    """e.g. "-1 C 200 / +1 C 210 2026-12-18"; per-leg expiries when they differ; stock "+100 sh"."""
    opt_exp = {l.expiry for l in legs if l.kind == "option"}
    single = len(opt_exp) == 1
    parts = []
    for l in legs:
        q = f"{'+' if l.qty > 0 else '-'}{_fmt_num(abs(l.qty))}"
        if l.kind == "stock":
            parts.append(f"{q} sh")
        else:
            s = f"{q} {l.right} {_fmt_num(l.strike)}"
            parts.append(s if single else f"{s} {l.expiry.isoformat()}")
    text = " / ".join(parts)
    return f"{text} {next(iter(opt_exp)).isoformat()}" if single else text


def net_cost(legs: Sequence[Leg]) -> float:
    """Entry net debit (+) / credit (−) in dollars."""
    return float(sum(l.qty * l.multiplier * (l.price or 0.0) for l in legs))


def close_dt(d: date) -> datetime:
    return datetime.combine(d, REGULAR_CLOSE, NY)


def value_at(legs: Sequence[Leg], prices, at: datetime, r: float, q: float, iv_shift: float = 0.0) -> np.ndarray:
    """Position market value ($) at time ``at`` for each underlying price. Options expiring at or
    before ``at`` take intrinsic value; others BSM with the leg's own IV (+ iv_shift)."""
    S = np.asarray(prices, dtype=float)
    total = np.zeros_like(S)
    for l in legs:
        if l.kind == "stock":
            total = total + l.qty * S
            continue
        expired = close_dt(l.expiry) <= at
        if expired:
            v = np.maximum(S - l.strike, 0.0) if l.right == "C" else np.maximum(l.strike - S, 0.0)
        else:
            if l.iv is None:
                raise ValueError(f"leg {l.right} {l.strike} {l.expiry} needs an IV")
            T = year_fraction_to_expiry(l.expiry, at)
            v = bs.price(S, l.strike, T, r, q, max(l.iv + iv_shift, 1e-4), l.right)
        total = total + l.qty * OPTION_MULT * v
    return total


def position_greeks(legs: Sequence[Leg], spot: float, at: datetime, r: float, q: float,
                    iv_shift: float = 0.0) -> dict:
    """Position totals: delta ($ per $1 = share-equivalents), gamma (per $1), theta ($/day),
    vega ($ per vol pt)."""
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l in legs:
        if l.kind == "stock":
            out["delta"] += l.qty
            continue
        if close_dt(l.expiry) <= at or l.iv is None:
            continue
        g = bs.greeks(spot, l.strike, year_fraction_to_expiry(l.expiry, at), r, q,
                      max(l.iv + iv_shift, 1e-4), l.right)
        for k in out:
            out[k] += l.qty * OPTION_MULT * g[k]
    return out


# ============================================================================ linear payoff kernel
def linear_profile(K: np.ndarray, is_call: np.ndarray, qty: np.ndarray, stock: float | np.ndarray,
                   debit: np.ndarray, S: float, T: np.ndarray, sigma: np.ndarray, mu: float,
                   sigma_hv: float | None = None) -> dict:
    """Exact expiry profile of single-expiry positions, vectorized over n candidates (per share).

    payoff(x) = Σ_j qty_j·intrinsic_j(x) + stock·x − debit. K (n, L); is_call, qty (L,) or (n, L).
    Returns arrays: max_profit (inf = unlimited), max_loss (inf = unlimited; positive = loss),
    breakevens (n, L+1, NaN-padded, ascending), pop, prob_max_profit (NaN if unlimited), ev_iv,
    ev_hv (NaN if sigma_hv is None).
    """
    K = np.atleast_2d(np.asarray(K, dtype=float))
    n, L = K.shape
    is_call = np.broadcast_to(np.asarray(is_call, bool), (n, L))
    qty = np.broadcast_to(np.asarray(qty, float), (n, L))
    stock = np.broadcast_to(np.asarray(stock, float), (n,))
    debit = np.broadcast_to(np.asarray(debit, float), (n,))
    T = np.broadcast_to(np.asarray(T, float), (n,))
    sigma = np.broadcast_to(np.asarray(sigma, float), (n,))

    x = np.concatenate([np.zeros((n, 1)), np.sort(K, axis=1)], axis=1)          # kinks (n, L+1)
    xx = x[:, :, None]
    intr = np.where(is_call[:, None, :], np.maximum(xx - K[:, None, :], 0.0), np.maximum(K[:, None, :] - xx, 0.0))
    vals = (intr * qty[:, None, :]).sum(axis=2) + stock[:, None] * x - debit[:, None]
    slope = (qty * is_call).sum(axis=1) + stock
    eps = 1e-12
    scale = np.maximum(1.0, np.abs(vals).max(axis=1))
    tol = 1e-9 * scale

    max_profit = np.where(slope > eps, np.inf, vals.max(axis=1))
    max_loss = np.where(slope < -eps, np.inf, -vals.min(axis=1))

    def pa(k):  # P(S_T > k) under IV measure
        return prob.prob_above(S, k, T[:, None], sigma[:, None], mu)

    a, b = vals[:, :-1], vals[:, 1:]
    xa, xb = x[:, :-1], x[:, 1:]
    pos_a, pos_b = a > tol[:, None], b > tol[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = np.where(pos_a != pos_b, xa + (xb - xa) * a / (a - b), np.nan)
    v_end, x_end = vals[:, -1], x[:, -1]
    with np.errstate(divide="ignore", invalid="ignore"):
        xc_end = np.where(((v_end > tol) & (slope < -eps)) | ((v_end <= tol) & (slope > eps)),
                          x_end - v_end / slope, np.nan)
    bes = np.concatenate([xc, xc_end[:, None]], axis=1)
    bes = np.sort(bes, axis=1)  # NaNs sort last

    # one vectorized CDF evaluation for kinks, interior crossings and the tail crossing
    allx = np.concatenate([x, np.where(np.isfinite(xc), xc, xa),
                           np.where(np.isfinite(xc_end), xc_end, x_end)[:, None]], axis=1)
    P_all = pa(allx)
    P_x = P_all[:, : L + 1]
    P_a, P_b, P_c = P_x[:, :-1], P_x[:, 1:], P_all[:, L + 1: 2 * L + 1]
    mass = np.where(pos_a & pos_b, P_a - P_b,
                    np.where(pos_a & ~pos_b, P_a - P_c, np.where(~pos_a & pos_b, P_c - P_b, 0.0)))
    P_end, P_cend = P_x[:, -1], P_all[:, -1]
    end_pos = v_end > tol
    end_mass = np.where(end_pos & (slope >= -eps), P_end,
                        np.where(end_pos, P_end - P_cend, np.where(slope > eps, P_cend, 0.0)))
    pop = mass.sum(axis=1) + end_mass

    finite_mp = np.isfinite(max_profit)
    mp = np.where(finite_mp, max_profit, 0.0)[:, None]
    at_max = (np.abs(a - mp) <= tol[:, None]) & (np.abs(b - mp) <= tol[:, None])
    pmax = np.where(at_max, P_a - P_b, 0.0).sum(axis=1)
    pmax += np.where((np.abs(v_end - mp[:, 0]) <= tol) & (np.abs(slope) <= eps), P_end, 0.0)
    pmax = np.where(finite_mp, pmax, np.nan)

    def ev(sig, m):
        fwd = prob.expected_spot(S, T, m)
        ec = prob.expected_call(S, K, T[:, None], sig, m)
        ep = ec - (fwd[:, None] - K)                     # put-call parity of expectations
        return (qty * np.where(is_call, ec, ep)).sum(axis=1) + stock * fwd - debit

    ev_iv = ev(sigma[:, None], mu)
    ev_hv = ev(sigma_hv, 0.0) if sigma_hv is not None else np.full(n, np.nan)
    return {"max_profit": max_profit, "max_loss": max_loss, "breakevens": bes, "pop": pop,
            "prob_max_profit": pmax, "ev_iv": ev_iv, "ev_hv": ev_hv}


def naked_margin(S, K, premium, is_call) -> np.ndarray:
    """Reg-T style naked short option margin per share (SPEC §6 for puts; calls mirrored):
    put  max(0.20·S − OTM + prem, 0.10·K + prem), OTM = max(0, S − K);
    call max(0.20·S − OTM + prem, 0.10·S + prem), OTM = max(0, K − S)."""
    S, K, premium = (np.asarray(a, float) for a in (S, K, premium))
    otm = np.where(is_call, np.maximum(0.0, K - S), np.maximum(0.0, S - K))
    floor = np.where(is_call, 0.10 * S, 0.10 * K)
    return np.maximum(0.20 * S - otm + premium, floor + premium)


# ============================================================================ templates
@dataclass(frozen=True)
class TLeg:
    kind: Literal["stock", "option"]
    qty: int                        # contracts (options) or shares (stock)
    right: Right | None = None
    steps: int = 0                  # strike = anchor(right) + steps·width (strike space)
    expiry_index: int = 0           # 0 = primary/front expiry, 1 = back expiry


@dataclass(frozen=True)
class Template:
    id: str
    label: str
    family: str
    description: str
    legs: tuple[TLeg, ...]
    otm_pct: float = 0.0            # default anchor distance OTM (calls above, puts below spot)

    def as_dict(self, width_pct: float = 0.05) -> dict:
        """StrategyTemplate (types.ts): strike_offset_pct relative to spot."""
        legs = []
        for l in self.legs:
            d: dict = {"kind": l.kind, "qty": l.qty}
            if l.kind == "option":
                sgn = 1.0 if l.right == "C" else -1.0
                d.update(right=l.right, strike_offset_pct=round(sgn * self.otm_pct + l.steps * width_pct, 6),
                         expiry_index=l.expiry_index)
            legs.append(d)
        return {"id": self.id, "label": self.label, "family": self.family,
                "description": self.description, "legs": legs}


def _o(right, qty, steps=0, exp=0):
    return TLeg("option", qty, right, steps, exp)


_STOCK = TLeg("stock", UNIT_SHARES)
_SHORT_STOCK = TLeg("stock", -UNIT_SHARES)

TEMPLATES: dict[str, Template] = {t.id: t for t in (
    Template("long-call", "Long call", "single", "Buy a call.", (_o("C", 1),)),
    Template("short-call", "Short call", "single", "Sell a naked call (unlimited risk).", (_o("C", -1),)),
    Template("long-put", "Long put", "single", "Buy a put.", (_o("P", 1),)),
    Template("short-put", "Short (cash-secured) put", "single", "Sell a put.", (_o("P", -1),)),
    Template("covered-call", "Covered call", "single", "Long 100 shares, short 1 call.", (_STOCK, _o("C", -1))),
    Template("protective-put", "Protective put", "single", "Long 100 shares, long 1 put.", (_STOCK, _o("P", 1))),
    Template("long-stock", "Long stock", "stock", "Buy 100 shares.", (_STOCK,)),
    Template("short-stock", "Short stock", "stock", "Short 100 shares.", (_SHORT_STOCK,)),
    Template("bull-call-spread", "Bull call spread", "vertical", "Debit: buy call, sell higher call.",
             (_o("C", 1, 0), _o("C", -1, 1))),
    Template("bear-put-spread", "Bear put spread", "vertical", "Debit: buy put, sell lower put.",
             (_o("P", 1, 0), _o("P", -1, -1))),
    Template("bull-put-spread", "Bull put spread", "vertical", "Credit: sell put, buy lower put.",
             (_o("P", -1, 0), _o("P", 1, -1))),
    Template("bear-call-spread", "Bear call spread", "vertical", "Credit: sell call, buy higher call.",
             (_o("C", -1, 0), _o("C", 1, 1))),
    Template("iron-condor-short", "Short iron condor", "condor_fly",
             "Credit: sell OTM put & call spreads (short inner, long outer).",
             (_o("P", 1, -1), _o("P", -1, 0), _o("C", -1, 0), _o("C", 1, 1)), otm_pct=0.05),
    Template("iron-condor-long", "Long iron condor", "condor_fly",
             "Debit: buy OTM put & call spreads (long inner, short outer).",
             (_o("P", -1, -1), _o("P", 1, 0), _o("C", 1, 0), _o("C", -1, 1)), otm_pct=0.05),
    Template("call-butterfly-long", "Long call butterfly", "condor_fly", "+1/−2/+1 calls, equal wings.",
             (_o("C", 1, -1), _o("C", -2, 0), _o("C", 1, 1))),
    Template("call-butterfly-short", "Short call butterfly", "condor_fly", "−1/+2/−1 calls, equal wings.",
             (_o("C", -1, -1), _o("C", 2, 0), _o("C", -1, 1))),
    Template("put-butterfly-long", "Long put butterfly", "condor_fly", "+1/−2/+1 puts, equal wings.",
             (_o("P", 1, -1), _o("P", -2, 0), _o("P", 1, 1))),
    Template("put-butterfly-short", "Short put butterfly", "condor_fly", "−1/+2/−1 puts, equal wings.",
             (_o("P", -1, -1), _o("P", 2, 0), _o("P", -1, 1))),
    Template("iron-butterfly-short", "Short iron butterfly", "condor_fly",
             "Credit: sell ATM straddle, buy wings.",
             (_o("P", 1, -1), _o("P", -1, 0), _o("C", -1, 0), _o("C", 1, 1))),
    Template("iron-butterfly-long", "Long iron butterfly", "condor_fly",
             "Debit: buy ATM straddle, sell wings.",
             (_o("P", -1, -1), _o("P", 1, 0), _o("C", 1, 0), _o("C", -1, 1))),
    Template("calendar-call", "Call calendar", "calendar", "Sell front call, buy back call, same strike.",
             (_o("C", -1, 0, 0), _o("C", 1, 0, 1))),
    Template("calendar-put", "Put calendar", "calendar", "Sell front put, buy back put, same strike.",
             (_o("P", -1, 0, 0), _o("P", 1, 0, 1))),
    Template("calendar-call-short", "Short call calendar", "calendar", "Buy front call, sell back call.",
             (_o("C", 1, 0, 0), _o("C", -1, 0, 1))),
    Template("calendar-put-short", "Short put calendar", "calendar", "Buy front put, sell back put.",
             (_o("P", 1, 0, 0), _o("P", -1, 0, 1))),
    Template("long-straddle", "Long straddle", "straddle", "Buy ATM call and put.", (_o("C", 1), _o("P", 1))),
    Template("short-straddle", "Short straddle", "straddle", "Sell ATM call and put.", (_o("C", -1), _o("P", -1))),
    Template("long-strangle", "Long strangle", "straddle", "Buy OTM call and put.",
             (_o("C", 1), _o("P", 1)), otm_pct=0.05),
    Template("short-strangle", "Short strangle", "straddle", "Sell OTM call and put.",
             (_o("C", -1), _o("P", -1)), otm_pct=0.05),
    Template("synthetic-long", "Synthetic long stock", "synthetic", "Buy call, sell put, same strike.",
             (_o("C", 1), _o("P", -1))),
)}


def strategy_templates() -> list[dict]:
    """StrategyTemplate[] for GET /api/strategy-templates."""
    return [t.as_dict() for t in TEMPLATES.values()]


def resolve_strikes(template: Template, spot: float, strikes_for: Callable[[int, Right], np.ndarray],
                    rule: str = "atm", value: float | None = None, width_pct: float | None = None,
                    delta_for: Callable[[int, Right, np.ndarray], np.ndarray] | None = None) -> list[float | None]:
    """Strike per template leg (None for stock legs).

    anchor(right): rule "atm" -> spot·(1 ± template.otm_pct); "pct_otm" v -> spot·(1 + v) for calls,
    spot·(1 − v) for puts; "delta" v -> listed strike whose |delta| is nearest v (needs delta_for).
    Leg target = anchor + steps·width_pct·spot (width default 5%), snapped to the nearest listed
    strike of that leg's expiry (strikes_for(expiry_index, right), ascending). A non-zero step that
    snaps onto the anchor's strike is pushed one listed strike outward (keeps spreads non-degenerate).
    """
    w = (width_pct if width_pct is not None else 0.05) * spot
    out: list[float | None] = []
    for l in template.legs:
        if l.kind == "stock":
            out.append(None)
            continue
        grid = np.asarray(strikes_for(l.expiry_index, l.right), dtype=float)
        if len(grid) == 0:
            out.append(None)
            continue
        sgn = 1.0 if l.right == "C" else -1.0
        if rule == "delta":
            if delta_for is None or value is None:
                raise ValueError("delta rule needs delta_for and value")
            d = np.abs(np.asarray(delta_for(l.expiry_index, l.right, grid), dtype=float))
            anchor = float(grid[int(np.nanargmin(np.abs(d - abs(value))))])
        elif rule == "pct_otm":
            anchor = spot * (1.0 + sgn * float(value or 0.0))
        else:
            anchor = spot * (1.0 + sgn * template.otm_pct)
        ia = int(np.argmin(np.abs(grid - anchor)))
        if l.steps == 0:
            out.append(float(grid[ia]))
            continue
        it = int(np.argmin(np.abs(grid - (grid[ia] + l.steps * w))))
        if it == ia:
            it = min(max(ia + int(np.sign(l.steps)), 0), len(grid) - 1)
        out.append(float(grid[it]) if it != ia else None)
    return out


# ============================================================================ screeners
@dataclass(frozen=True)
class ScreenParams:
    dte_min: int = 7
    dte_max: int = 60
    min_oi: int = 0
    min_volume: int = 0
    max_spread_pct: float | None = None      # (ask − bid)/mid, per leg
    require_bid: bool = True                 # bid > 0 on every leg
    pricing: Literal["natural", "mid"] = "natural"
    earnings_filter: Literal["any", "exclude", "include"] = "any"
    delta_min: float | None = None           # |delta| of the anchor leg(s)
    delta_max: float | None = None
    max_width_steps: int = 5                 # spreads/flies: up to N listed (liquid) strikes apart
    width_min_pct: float | None = None       # spread width as a fraction of spot
    width_max_pct: float | None = None
    back_gap_min: int = 20                   # calendars: back DTE − front DTE window (days)
    back_gap_max: int = 70
    side: Literal["long", "short"] = "long"  # straddles / strangles


@dataclass(frozen=True)
class _Spec:
    id: str
    template: str
    is_call: tuple[bool, ...]
    qty: tuple[float, ...]
    stock: float = 0.0              # shares per share-unit (1 => 100 shares per unit)
    calendar: bool = False


def _spec(sid: str, tid: str) -> _Spec:
    t = TEMPLATES[tid]
    opts = [l for l in t.legs if l.kind == "option"]
    stock = sum(l.qty for l in t.legs if l.kind == "stock") / UNIT_SHARES
    return _Spec(sid, tid, tuple(l.right == "C" for l in opts), tuple(float(l.qty) for l in opts), stock,
                 t.family == "calendar")


SCREENERS: dict[str, str] = {
    "covered-calls": "covered-call", "naked-puts": "short-put", "long-calls": "long-call",
    "long-puts": "long-put", "bull-call-spreads": "bull-call-spread", "bear-put-spreads": "bear-put-spread",
    "bull-put-spreads": "bull-put-spread", "bear-call-spreads": "bear-call-spread",
    "iron-condors-short": "iron-condor-short", "iron-condors-long": "iron-condor-long",
    "call-butterflies-long": "call-butterfly-long", "call-butterflies-short": "call-butterfly-short",
    "put-butterflies-long": "put-butterfly-long", "put-butterflies-short": "put-butterfly-short",
    "iron-butterflies-short": "iron-butterfly-short", "iron-butterflies-long": "iron-butterfly-long",
    "calendar-calls": "calendar-call", "calendar-puts": "calendar-put",
    "atm-straddles": "long-straddle", "strangles": "long-strangle", "synthetic-long": "synthetic-long",
}
SCREENER_IDS = tuple(SCREENERS)
FAMILY_OF = {sid: TEMPLATES[tid].family for sid, tid in SCREENERS.items()}


class _Ctx:
    """Per-chain precomputation shared by all generators."""

    def __init__(self, ca: ChainArrays, p: ScreenParams):
        self.ca, self.p = ca, p
        bid, ask = ca.bid, ca.ask
        qmid = (bid + ask) / 2.0
        with np.errstate(divide="ignore", invalid="ignore"):
            self.spread_pct = np.where(qmid > 0, (ask - bid) / qmid, np.inf)
        liq = np.isfinite(ca.mid) & (ask > 0) & (ask >= bid) & (ca.oi >= p.min_oi) & (ca.volume >= p.min_volume)
        if p.require_bid:
            liq &= bid > 0
        else:
            liq &= bid >= 0
        if p.max_spread_pct is not None:
            liq &= self.spread_pct <= p.max_spread_pct
        self.liq = liq
        self.in_dte = (ca.dte >= p.dte_min) & (ca.dte <= p.dte_max)
        # greeks: feed values, BSM fill where missing
        self.g = {k: getattr(ca, k).copy() for k in ("delta", "gamma", "theta", "vega")}
        miss = ~(np.isfinite(self.g["delta"]) & np.isfinite(self.g["gamma"]) & np.isfinite(self.g["theta"])
                 & np.isfinite(self.g["vega"])) & np.isfinite(ca.iv)
        if miss.any():
            g = bs.greeks(ca.spot, ca.strike[miss], ca.t[miss], ca.r, ca.q, ca.iv[miss], ca.is_call[miss])
            for k in self.g:
                feed = self.g[k][miss]
                self.g[k][miss] = np.where(np.isfinite(feed), feed, g[k])
        self.atm_by_exp = vol.atm_iv_array(ca)
        self.key = ca.exp_idx.astype(np.int64) * 10**10 + np.round(ca.strike * 1000).astype(np.int64)

    def rows(self, call: bool, dte_window: bool = True) -> np.ndarray:
        m = self.liq & (self.ca.is_call == call)
        if dte_window:
            m &= self.in_dte
        return np.nonzero(m)[0]

    def lookup(self, subset: np.ndarray, exp_idx: np.ndarray, strike: np.ndarray) -> np.ndarray:
        """Row index in ``subset`` (sorted by expiry, strike) matching (exp_idx, strike), else −1."""
        keys = self.key[subset]
        want = exp_idx.astype(np.int64) * 10**10 + np.round(strike * 1000).astype(np.int64)
        pos = np.clip(np.searchsorted(keys, want), 0, max(len(keys) - 1, 0))
        if len(keys) == 0:
            return np.full(len(want), -1)
        return np.where(keys[pos] == want, subset[pos], -1)

    def delta_ok(self, rows: np.ndarray) -> np.ndarray:
        d = np.abs(self.g["delta"][rows])
        ok = np.ones(len(rows), bool)
        if self.p.delta_min is not None:
            ok &= d >= self.p.delta_min
        if self.p.delta_max is not None:
            ok &= d <= self.p.delta_max
        return ok

    def width_ok(self, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        w = (self.ca.strike[hi] - self.ca.strike[lo]) / self.ca.spot
        ok = w > 0
        if self.p.width_min_pct is not None:
            ok &= w >= self.p.width_min_pct - 1e-12
        if self.p.width_max_pct is not None:
            ok &= w <= self.p.width_max_pct + 1e-12
        return ok

    def nearest_delta(self, subset: np.ndarray, exp_idx: np.ndarray, target_abs: np.ndarray) -> np.ndarray:
        """Row in ``subset`` (single right) of the same expiry with |delta| nearest target, else −1."""
        out = np.full(len(exp_idx), -1)
        if len(subset) == 0:
            return out
        d = np.abs(self.g["delta"][subset])
        e = self.ca.exp_idx[subset]
        for ei in np.unique(exp_idx):
            cand = subset[(e == ei) & np.isfinite(d)]
            if len(cand) == 0:
                continue
            dc = np.abs(self.g["delta"][cand])
            sel = exp_idx == ei
            j = np.argmin(np.abs(dc[None, :] - target_abs[sel][:, None]), axis=1)
            out[sel] = cand[j]
        return out


def _pairs(sub: np.ndarray, ca: ChainArrays, max_steps: int) -> tuple[np.ndarray, np.ndarray]:
    """(lo, hi) row pairs of the same expiry, hi up to max_steps listed strikes above lo."""
    los, his = [], []
    for o in range(1, max_steps + 1):
        if o >= len(sub):
            break
        a, b = sub[:-o], sub[o:]
        same = ca.exp_idx[a] == ca.exp_idx[b]
        los.append(a[same])
        his.append(b[same])
    if not los:
        return np.zeros(0, int), np.zeros(0, int)
    return np.concatenate(los), np.concatenate(his)


def _gen(spec: _Spec, x: _Ctx) -> np.ndarray:
    """Candidate leg row indices (n, L) in template option-leg order."""
    ca, p = x.ca, x.p
    sid = spec.id
    calls, puts = x.rows(True), x.rows(False)
    if sid in ("covered-calls", "long-calls"):
        r = calls[x.delta_ok(calls)]
        return r[:, None]
    if sid in ("naked-puts", "long-puts"):
        r = puts[x.delta_ok(puts)]
        return r[:, None]
    if sid in ("bull-call-spreads", "bear-call-spreads", "bull-put-spreads", "bear-put-spreads"):
        sub = calls if spec.is_call[0] else puts
        lo, hi = _pairs(sub, ca, p.max_width_steps)
        keep = x.width_ok(lo, hi)
        lo, hi = lo[keep], hi[keep]
        # template leg order: first leg is the anchor (long for debit, short for credit)
        if sid == "bull-call-spreads":
            legs = (lo, hi)           # +C lo, −C hi
        elif sid == "bear-call-spreads":
            legs = (lo, hi)           # −C lo, +C hi
        elif sid == "bull-put-spreads":
            legs = (hi, lo)           # −P hi, +P lo
        else:
            legs = (hi, lo)           # +P hi, −P lo
        anchor = legs[0]
        ok = x.delta_ok(anchor)
        return np.stack([legs[0][ok], legs[1][ok]], axis=1)
    if sid in ("iron-condors-short", "iron-condors-long"):
        plo, phi = _pairs(puts, ca, p.max_width_steps)    # inner put = phi
        keep = x.width_ok(plo, phi) & x.delta_ok(phi) & (ca.strike[phi] < ca.spot)
        plo, phi = plo[keep], phi[keep]
        otm_calls = calls[ca.strike[calls] > ca.spot]
        cin = x.nearest_delta(otm_calls, ca.exp_idx[phi], np.abs(x.g["delta"][phi]))
        good = cin >= 0
        plo, phi, cin = plo[good], phi[good], cin[good]
        cout = x.lookup(calls, ca.exp_idx[cin], ca.strike[cin] + (ca.strike[phi] - ca.strike[plo]))
        good = (cout >= 0) & x.delta_ok(cin)
        return np.stack([plo[good], phi[good], cin[good], cout[good]], axis=1)
    if sid in ("call-butterflies-long", "call-butterflies-short", "put-butterflies-long", "put-butterflies-short"):
        sub = calls if spec.is_call[0] else puts
        lo, mid = _pairs(sub, ca, p.max_width_steps)
        hi = x.lookup(sub, ca.exp_idx[mid], 2 * ca.strike[mid] - ca.strike[lo])
        good = (hi >= 0) & x.width_ok(lo, mid)
        lo, mid, hi = lo[good], mid[good], hi[good]
        ok = x.delta_ok(mid)
        return np.stack([lo[ok], mid[ok], hi[ok]], axis=1)
    if sid in ("iron-butterflies-short", "iron-butterflies-long"):
        pc = x.lookup(puts, ca.exp_idx[calls], ca.strike[calls])       # put at the call's strike
        good = pc >= 0
        cc, pc = calls[good], pc[good]
        ok = x.delta_ok(cc)
        cc, pc = cc[ok], pc[ok]
        out = []
        pos = np.searchsorted(puts, pc)
        for o in range(1, p.max_width_steps + 1):
            j = pos - o
            valid = j >= 0
            pl = np.where(valid, puts[np.clip(j, 0, None)], -1)
            valid &= (pl >= 0) & (ca.exp_idx[np.clip(pl, 0, None)] == ca.exp_idx[pc])
            w = ca.strike[pc] - ca.strike[np.clip(pl, 0, None)]
            cl = x.lookup(calls, ca.exp_idx[cc], ca.strike[cc] + w)
            valid &= cl >= 0
            valid &= x.width_ok(np.clip(pl, 0, None), pc)
            out.append(np.stack([pl[valid], pc[valid], cc[valid], cl[valid]], axis=1))
        return np.concatenate(out) if out else np.zeros((0, 4), int)
    if sid in ("calendar-calls", "calendar-puts"):
        front = calls if spec.is_call[0] else puts
        front = front[x.delta_ok(front)]
        allr = x.rows(spec.is_call[0], dte_window=False)
        # back leg = the nearest later expiry (same strike, liquid) whose DTE gap is in the window
        chosen = np.full(len(front), -1)
        for j in range(1, len(ca.expiries)):
            todo = chosen < 0
            if not todo.any():
                break
            back = x.lookup(allr, ca.exp_idx[front] + j, ca.strike[front])
            gap = np.where(back >= 0, ca.dte[np.clip(back, 0, None)] - ca.dte[front], -1)
            ok = todo & (back >= 0) & (gap >= p.back_gap_min) & (gap <= p.back_gap_max)
            chosen[ok] = back[ok]
        good = chosen >= 0
        return np.stack([front[good], chosen[good]], axis=1)
    if sid == "atm-straddles":
        pc = x.lookup(puts, ca.exp_idx[calls], ca.strike[calls])
        good = pc >= 0
        cc, pc = calls[good], pc[good]
        dist = np.abs(ca.strike[cc] - ca.spot)
        e = ca.exp_idx[cc]
        best = []
        for ei in np.unique(e):
            sel = np.nonzero(e == ei)[0]
            best.append(sel[np.argmin(dist[sel])])
        best = np.array(best, dtype=int)
        return np.stack([cc[best], pc[best]], axis=1) if len(best) else np.zeros((0, 2), int)
    if sid == "strangles":
        op = puts[(ca.strike[puts] < ca.spot)]
        op = op[x.delta_ok(op)]
        oc = calls[ca.strike[calls] > ca.spot]
        cc = x.nearest_delta(oc, ca.exp_idx[op], np.abs(x.g["delta"][op]))
        good = cc >= 0
        return np.stack([cc[good], op[good]], axis=1)       # template order: C, P
    if sid == "synthetic-long":
        pc = x.lookup(puts, ca.exp_idx[calls], ca.strike[calls])
        good = pc >= 0
        cc, pc = calls[good], pc[good]
        ok = x.delta_ok(cc)
        return np.stack([cc[ok], pc[ok]], axis=1)
    raise KeyError(sid)


def _nan_to_none(v):
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


@dataclass
class ScreenResult:
    """Columnar screener output for one strategy (one or many symbols). ``rows()`` materializes
    dicts with the documented keys; ``cols`` holds 1-D arrays (``strikes``/``be`` are 2-D)."""
    strategy: str
    cols: dict[str, np.ndarray]
    leg_is_call: tuple[bool, ...]
    leg_qty: tuple[float, ...]
    leg_exp: tuple[int, ...]               # 0 = expiry, 1 = back_expiry
    stock: float

    def __len__(self) -> int:
        return len(self.cols["dte"])

    def take(self, idx: np.ndarray) -> "ScreenResult":
        return replace(self, cols={k: v[idx] for k, v in self.cols.items()})

    @staticmethod
    def concat(results: Sequence["ScreenResult"]) -> "ScreenResult | None":
        results = [r for r in results if r is not None]
        if not results:
            return None
        r0 = results[0]
        cols = {k: np.concatenate([r.cols[k] for r in results]) for k in r0.cols}
        return replace(r0, cols=cols)

    def order_by(self, key: str, descending: bool = True) -> np.ndarray:
        v = np.asarray(self.cols[key], dtype=float)
        v = np.where(np.isfinite(v), v, -np.inf if descending else np.inf)
        o = np.argsort(-v if descending else v, kind="stable")
        return o

    def rows(self, idx: Iterable[int] | None = None) -> list[dict]:
        sel = np.arange(len(self)) if idx is None else np.asarray(list(idx), dtype=int)
        c = {k: v[sel] for k, v in self.cols.items()}
        lists = {k: (v.tolist() if v.ndim == 1 else [list(x) for x in v.tolist()]) for k, v in c.items()}
        out = []
        n_be = c["be"].shape[1] if len(sel) else 0
        for i in range(len(sel)):
            row = {}
            for k, v in lists.items():
                if k in ("be", "strikes"):
                    continue
                row[k] = _nan_to_none(v[i])
            strikes = lists["strikes"][i]
            bes = [b for b in lists["be"][i] if math.isfinite(b)] if n_be else []
            row["strikes"] = strikes
            row["breakevens"] = bes
            row["breakeven_low"] = bes[0] if bes else None
            row["breakeven_high"] = bes[-1] if bes else None
            legs = []
            if self.stock:
                legs.append(Leg("stock", self.stock * UNIT_SHARES))
            for j, (ic, q, e) in enumerate(zip(self.leg_is_call, self.leg_qty, self.leg_exp)):
                legs.append(Leg("option", q, "C" if ic else "P", strikes[j],
                                row["expiry"] if e == 0 else row["back_expiry"]))
            row["legs"] = describe_legs(legs)
            out.append(row)
        return out


def _row_nanmean(a: np.ndarray) -> np.ndarray:
    """Row mean ignoring NaN (NaN for all-NaN rows), without RuntimeWarnings."""
    ok = np.isfinite(a)
    cnt = ok.sum(axis=1)
    tot = np.where(ok, a, 0.0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(cnt > 0, tot / cnt, np.nan)


def screen(strategy_id: str, ca: ChainArrays, params: ScreenParams = ScreenParams()) -> ScreenResult | None:
    """Run one strategy screener over one symbol's chain. None when no candidates."""
    tid = SCREENERS[strategy_id]
    spec = _spec(strategy_id, tid)
    if strategy_id in ("atm-straddles", "strangles") and params.side == "short":
        spec = replace(spec, qty=tuple(-q for q in spec.qty))
    if len(ca) == 0 or not ca.spot or ca.spot <= 0:
        return None
    x = _Ctx(ca, params)
    idx = _gen(spec, x)
    if idx.size == 0:
        return None
    n, L = idx.shape
    S, r, q = ca.spot, ca.r, ca.q
    qty = np.array(spec.qty)
    is_call = np.array(spec.is_call)
    leg_exp = (0, 1) if spec.calendar else (0,) * L

    K = ca.strike[idx]
    bid, ask, mid = ca.bid[idx], ca.ask[idx], ca.mid[idx]
    nat = np.where(qty > 0, ask, bid)
    price_nat = (qty * nat).sum(axis=1) + spec.stock * S
    price_mid = (qty * mid).sum(axis=1) + spec.stock * S
    price = price_nat if params.pricing == "natural" else price_mid
    e0 = ca.exp_idx[idx[:, 0]]
    T = ca.t[idx[:, 0]]
    dte = ca.dte[idx[:, 0]]
    atm = x.atm_by_exp[e0]
    leg_iv = ca.iv[idx]
    mean_iv = _row_nanmean(leg_iv)
    sigma = np.where(np.isfinite(atm), atm, mean_iv)
    mu = r - q
    hv = ca.hv30

    cols: dict[str, np.ndarray] = {}
    if not spec.calendar:
        prof = linear_profile(K, is_call, qty, spec.stock, price, S, T, sigma, mu, hv)
        mp, ml = prof["max_profit"], prof["max_loss"]
        be = prof["breakevens"]
        pop, pmax, ev_iv, ev_hv = prof["pop"], prof["prob_max_profit"], prof["ev_iv"], prof["ev_hv"]
    else:
        prof = _calendar_profile(ca, idx, qty, is_call, price, sigma, hv)
        mp, ml, be = prof["max_profit"], prof["max_loss"], prof["breakevens"]
        pop, pmax, ev_iv, ev_hv = prof["pop"], prof["prob_max_profit"], prof["ev_iv"], prof["ev_hv"]

    valid = np.isfinite(price) & np.isfinite(sigma) & (sigma > 0)
    unit = OPTION_MULT
    mp_d = np.where(np.isinf(mp), np.nan, mp * unit)
    ml_d = np.where(np.isinf(ml), np.nan, ml * unit)
    with np.errstate(divide="ignore", invalid="ignore"):
        ror = np.where(np.isfinite(mp_d) & np.isfinite(ml_d) & (ml_d > 0), mp_d / ml_d, np.nan)
        ann = np.where(dte > 0, ror * 365.0 / dte, np.nan)

    g = {k: (qty * np.nan_to_num(x.g[k][idx])).sum(axis=1) * unit for k in ("delta", "gamma", "theta", "vega")}
    g["delta"] = g["delta"] + spec.stock * UNIT_SHARES
    last_exp = ca.exp_idx[idx[:, -1]] if spec.calendar else e0
    exp_arr = np.array(ca.expiries, dtype=object)
    earn = ca.next_earnings
    if earn is not None and earn >= ca.asof_date:
        earn_flag = np.array([earn <= e for e in ca.expiries])[last_exp]
    else:
        earn_flag = np.zeros(n, bool)

    cols.update(
        symbol=np.full(n, ca.symbol, dtype=object), strategy=np.full(n, strategy_id, dtype=object),
        expiry=exp_arr[e0], back_expiry=exp_arr[ca.exp_idx[idx[:, 1]]] if spec.calendar else np.full(n, None, dtype=object),
        dte=dte.astype(int), spot=np.full(n, S), strikes=K,
        price=price, price_natural=price_nat, price_mid=price_mid,
        pricing=np.full(n, params.pricing, dtype=object), cost=price * unit, is_credit=price < 0,
        max_profit=mp_d, max_loss=ml_d, be=be, return_on_risk=ror, annualized_return=ann,
        pop=pop, prob_max_profit=pmax, ev_iv=ev_iv * unit, ev_hv=ev_hv * unit,
        delta=g["delta"], gamma=g["gamma"], theta=g["theta"], vega=g["vega"],
        iv=mean_iv,
        atm_iv=atm, iv_rank=np.full(n, np.nan if ca.iv_rank is None else ca.iv_rank),
        hv30=np.full(n, np.nan if hv is None else hv), earnings_before_expiry=earn_flag,
        next_earnings=np.full(n, earn, dtype=object),
        min_leg_oi=ca.oi[idx].min(axis=1).astype(int), min_leg_volume=ca.volume[idx].min(axis=1).astype(int),
        max_leg_spread_pct=x.spread_pct[idx].max(axis=1),
    )
    cols.update(_extras(strategy_id, ca, idx, price, K, T, dte, sigma, x))

    # a candidate with no attainable profit (e.g. a short fly priced at natural) is never shown
    keep = valid & ~(np.isfinite(mp) & (mp <= 0))
    if params.earnings_filter == "exclude":
        keep &= ~earn_flag
    elif params.earnings_filter == "include":
        keep &= earn_flag
    if not keep.any():
        return None
    res = ScreenResult(strategy_id, cols, tuple(bool(c) for c in is_call), tuple(float(v) for v in qty),
                       leg_exp, spec.stock)
    return res.take(np.nonzero(keep)[0]) if not keep.all() else res


def _extras(sid: str, ca: ChainArrays, idx, price, K, T, dte, sigma, x: _Ctx) -> dict:
    S, mu = ca.spot, ca.r - ca.q
    with np.errstate(divide="ignore", invalid="ignore"):
        ann_f = np.where(dte > 0, 365.0 / dte, np.nan)
        if sid == "covered-calls":
            prem = S - price                       # call premium received (price = S − prem)
            k = K[:, 0]
            static = prem / (S - prem)
            called = (k - S + prem) / (S - prem)
            return {"static_return": static, "static_annualized": static * ann_f,
                    "if_called_return": called, "if_called_annualized": called * ann_f,
                    "downside_protection": prem / S,
                    "prob_called": prob.prob_above(S, k, T, sigma, mu)}
        if sid == "naked-puts":
            prem = -price
            k = K[:, 0]
            margin = naked_margin(S, k, prem, False) * OPTION_MULT
            roc = prem / (k - prem)
            rom = prem * OPTION_MULT / margin
            return {"return_on_cash": roc, "return_on_cash_annualized": roc * ann_f, "margin_req": margin,
                    "return_on_margin": rom, "return_on_margin_annualized": rom * ann_f,
                    "prob_otm": prob.prob_above(S, k, T, sigma, mu)}
        if sid in ("calendar-calls", "calendar-puts"):
            f, b = idx[:, 0], idx[:, 1]
            fiv, biv = ca.iv[f], ca.iv[b]
            tf, tb = ca.t[f], ca.t[b]
            fwd_var = (biv**2 * tb - fiv**2 * tf) / (tb - tf)
            th_f, th_b = x.g["theta"][f], x.g["theta"][b]
            est = bs.price(K[:, 0], K[:, 0], tb - tf, ca.r, ca.q, np.nan_to_num(biv, nan=1e-4), ca.is_call[b]) - price
            return {"front_iv": fiv, "back_iv": biv, "iv_ratio": fiv / biv,
                    "forward_vol": np.where(fwd_var >= 0, np.sqrt(np.abs(fwd_var)), np.nan),
                    "theta_ratio": th_f / th_b,
                    "est_max_profit": np.where(np.isfinite(biv), est * OPTION_MULT, np.nan)}
        if sid == "synthetic-long":
            c, p = idx[:, 0], idx[:, 1]
            k = K[:, 0]
            carry = S * np.exp(-ca.q * T) - k * np.exp(-ca.r * T)
            return {"synthetic_price_natural": k + ca.ask[c] - ca.bid[p],
                    "synthetic_price_mid": k + ca.mid[c] - ca.mid[p],
                    "parity_gap_natural": carry - (ca.ask[c] - ca.bid[p]),
                    "parity_gap_mid": carry - (ca.mid[c] - ca.mid[p])}
    return {}


CALENDAR_GRID_POINTS = 201


def _calendar_profile(ca: ChainArrays, idx, qty, is_call, price, sigma, hv, n_grid: int = CALENDAR_GRID_POINTS) -> dict:
    """Calendar P/L at the front expiry on the lognormal grid: back leg valued by BSM with its own
    IV for the remaining time; front leg intrinsic. Max loss = net debit (est.), max profit = grid
    maximum, breakevens from grid sign changes."""
    f, b = idx[:, 0], idx[:, 1]
    K = ca.strike[f]
    tf, tb = ca.t[f], ca.t[b]
    biv = np.nan_to_num(ca.iv[b], nan=np.nan)
    mu = ca.r - ca.q
    c = ca.is_call[f]

    def pnl(prices):
        kk, cc = K[:, None], c[:, None]
        front = np.where(cc, np.maximum(prices - kk, 0.0), np.maximum(kk - prices, 0.0))
        back = bs.price(prices, kk, (tb - tf)[:, None], ca.r, ca.q, np.nan_to_num(biv, nan=1e-4)[:, None], cc)
        return qty[0] * front + qty[1] * back - price[:, None]

    z, P = prob.grid(ca.spot, tf, sigma, mu, n=n_grid)
    vals = pnl(P)
    pop, ev_iv = prob.pop_ev_on_grid(z, vals)
    if hv is not None:
        zh, Ph = prob.grid(ca.spot, tf, hv, 0.0, n=n_grid)
        _, ev_hv = prob.pop_ev_on_grid(zh, pnl(Ph))
    else:
        ev_hv = np.full(len(f), np.nan)
    s = np.sign(np.where(vals > 0, 1.0, -1.0))
    cross = np.diff(s, axis=1) != 0
    a, bb = vals[:, :-1], vals[:, 1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = np.where(cross, P[:, :-1] + (P[:, 1:] - P[:, :-1]) * a / (a - bb), np.nan)
    xc = np.sort(xc, axis=1)[:, :2]
    long_cal = qty[1] > 0
    # long calendar: worst case is losing the debit (both legs → same value at extremes)
    max_loss = np.where(long_cal, np.maximum(price, 0.0), np.inf)
    peak = np.maximum(vals.max(axis=1), pnl(K[:, None])[:, 0])   # long calendar peaks at the strike
    max_profit = np.where(long_cal, peak, np.maximum(-price, 0.0))
    return {"max_profit": max_profit, "max_loss": max_loss, "breakevens": xc, "pop": pop,
            "prob_max_profit": np.full(len(f), np.nan), "ev_iv": ev_iv, "ev_hv": ev_hv}


def screen_universe(strategy_id: str, chains: Iterable[ChainArrays], params: ScreenParams = ScreenParams(),
                    sort_key: str = "ev_hv", descending: bool = True, limit: int | None = 5000) -> tuple[list[dict], bool]:
    """Screen many symbols, sort, and materialize the top ``limit`` rows. Returns (rows, truncated)."""
    res = ScreenResult.concat([screen(strategy_id, ca, params) for ca in chains])
    if res is None:
        return [], False
    order = res.order_by(sort_key, descending)
    truncated = limit is not None and len(order) > limit
    return res.rows(order[:limit] if limit is not None else order), truncated


# ============================================================================ trade ideas
IDEA_STRATEGIES = ("bull-call-spreads", "bear-put-spreads", "bull-put-spreads", "bear-call-spreads",
                   "iron-condors-short", "iron-butterflies-short", "call-butterflies-long",
                   "put-butterflies-long", "long-calls", "long-puts", "atm-straddles", "strangles",
                   "calendar-calls", "calendar-puts")
_IDEA_FAMILY = {"single": "single", "vertical": "vertical", "condor_fly": "condor_fly",
                "straddle": "straddle", "calendar": "calendar"}


def _idea(row: dict) -> dict:
    ml = row["max_loss"]
    edge = row["ev_hv"] / ml if row["ev_hv"] is not None and ml is not None and ml > 0 else None
    tid = SCREENERS[row["strategy"]]
    return {"strategy": row["strategy"], "label": TEMPLATES[tid].label,
            "family": _IDEA_FAMILY.get(FAMILY_OF[row["strategy"]], "single"), "legs": row["legs"],
            "expiry": row["expiry"], "cost": row["cost"], "max_profit": row["max_profit"], "max_loss": ml,
            "pop": row["pop"], "ev_hv": row["ev_hv"], "edge": edge, "note": None}


def rank_trade_ideas(ca: ChainArrays, params: ScreenParams = ScreenParams(), n: int = 10,
                     strategies: Sequence[str] = IDEA_STRATEGIES, families: Sequence[str] | None = None,
                     seasonality: dict | None = None) -> dict:
    """TradeIdeas for one symbol: {by_edge, by_win_rate} lists of TradeIdea dicts.

    edge = EV(HV)/max_loss (only finite-risk ideas with a known HV30); win rate = PoP. Candidates
    come from the strategy screeners; ``families`` filters (vertical/condor_fly/single/straddle/
    calendar). ``seasonality`` (a Seasonality.by_month row for the current month, keys month, avg,
    median, pos_pct, n) appends a seasonality idea to both lists when n ≥ 5.
    """
    by_edge, by_pop = [], []
    for sid in strategies:
        fam = _IDEA_FAMILY.get(FAMILY_OF[sid], "single")
        if families and fam not in families:
            continue
        res = screen(sid, ca, params)
        if res is None:
            continue
        ml = res.cols["max_loss"]
        risk_ok = np.isfinite(ml) & (ml > 0)
        e = np.where(risk_ok, res.cols["ev_hv"] / np.where(risk_ok, ml, 1.0), np.nan)
        res.cols["_edge"] = e
        res.cols["_pop"] = np.where(risk_ok, res.cols["pop"], np.nan)
        for key, bucket in (("_edge", by_edge), ("_pop", by_pop)):
            o = res.order_by(key)
            o = [i for i in o[:n] if np.isfinite(res.cols[key][i])]
            bucket.extend(_idea(r) for r in res.rows(o))
    by_edge.sort(key=lambda d: -d["edge"])
    by_pop.sort(key=lambda d: (-(d["pop"] or 0.0), -(d["edge"] if d["edge"] is not None else -np.inf)))
    by_edge, by_pop = by_edge[:n], by_pop[:n]
    if seasonality and seasonality.get("n", 0) >= 5 and seasonality.get("avg") is not None:
        bull = seasonality["avg"] > 0
        idea = {"strategy": "seasonality", "label": "Seasonal " + ("long" if bull else "short"),
                "family": "seasonality", "legs": "+100 sh" if bull else "-100 sh", "expiry": None,
                "cost": None, "max_profit": None, "max_loss": None, "pop": seasonality.get("pos_pct") if bull
                else (1 - seasonality["pos_pct"] if seasonality.get("pos_pct") is not None else None),
                "ev_hv": None, "edge": None,
                "note": f"Month {seasonality['month']}: avg {seasonality['avg']:.2%} over {seasonality['n']} years"}
        by_edge.append(idea)
        by_pop.append(idea)
    return {"symbol": ca.symbol, "by_edge": by_edge, "by_win_rate": by_pop}
