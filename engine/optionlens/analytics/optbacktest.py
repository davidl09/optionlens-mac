"""Option strategy backtests (SPEC §0.3, §7 earnings-option-strategy-screener, §8 straddle, §9
/backtest/options and /backtest/straddle-wing).

Pricing per leg and day (at the close): the caller's ``PriceProvider.real_mid`` when it has a
stored EOD mid, else a BSM model price with ``iv_proxy`` (the provider implements the §0.3 proxy
chain) and the caller's r/q. An option at/after its expiry close is worth intrinsic (exact, counted
as real). A trade is ``pricing: "real"`` only if every leg at entry and at exit was real.
When the provider lists no real expiries/strikes for a date, expiries are synthesized (weekly
Fridays for 9 weeks + third-Friday monthlies, holiday Fridays moved to Thursday) and strikes are
snapped to a listing-like grid ($1 < $50, $2.5 < $100, $5 < $500, $10 above; indices $5 below
3000, else $25).

Returns: P/L / basis where basis = net debit for debit trades, else the position's max risk
(finite expiry max loss; otherwise Reg-T style naked margin, short stock 100% of value).
Units: dollars for ``qty`` units (1 contract per option leg, 100 shares per stock leg).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Callable, Mapping, NamedTuple, Protocol, Sequence

import numpy as np

from . import bs
from . import calendar as cal
from .backtest import build_result
from .earnings import UNKNOWN_TIMING_NOTE, event_variance, reaction_log_moves, reaction_window
from .stats import BarArrays, bar_arrays
from .strategies import (OPTION_MULT, TEMPLATES, Leg, Template, close_dt, describe_legs, linear_profile,
                         naked_margin, net_cost, resolve_strikes)

Rate = float | Callable[[date], float]


class PriceProvider(Protocol):
    """Implemented by the services layer on top of option_eod / metrics_daily.

    Required: ``real_mid`` (stored EOD mid or None) and ``iv_proxy`` (SPEC §0.3 chain: stored ATM IV
    for the tenor → stored IV30 → HV30·k; None if nothing is known).
    Optional (looked up with getattr; absent = no real listing / no spread check):
    ``listed_expiries(d, symbol) -> Sequence[date]``,
    ``listed_strikes(d, symbol, expiry, right) -> Sequence[float]``,
    ``real_spread_pct(d, symbol, expiry, strike, right) -> float | None``  ((ask−bid)/mid).
    """

    def real_mid(self, d: date, symbol: str, expiry: date, strike: float, right: str) -> float | None: ...

    def iv_proxy(self, d: date, symbol: str, tenor_days: int) -> float | None: ...


# ============================================================================ listing synthesis
def strike_step(spot: float, is_index: bool = False) -> float:
    if is_index:
        return 5.0 if spot < 3000 else 25.0
    if spot < 50:
        return 1.0
    if spot < 100:
        return 2.5
    if spot < 500:
        return 5.0
    return 10.0


def synth_strikes(spot: float, is_index: bool = False, span: float = 0.5) -> np.ndarray:
    step = strike_step(spot, is_index)
    lo = max(step, math.floor(spot * (1 - span) / step) * step)
    hi = math.ceil(spot * (1 + span) / step) * step
    return np.arange(lo, hi + step / 2, step)


def _friday_expiry(f: date) -> date:
    return f if cal.is_trading_day(f) else cal.prev_trading_day(f)


@lru_cache(maxsize=4096)
def synth_expiries(d: date, horizon_days: int = 400) -> tuple[date, ...]:
    """Weekly Fridays for 9 weeks after d plus third-Friday monthlies to the horizon (holiday
    Fridays → previous trading day); all strictly after d."""
    out = set()
    f = d + timedelta(days=(4 - d.weekday()) % 7 or 7)
    for i in range(9):
        out.add(_friday_expiry(f + timedelta(weeks=i)))
    y, m = d.year, d.month
    while True:
        tf = cal.third_friday(y, m)
        if (tf - d).days > horizon_days:
            break
        out.add(_friday_expiry(tf))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return tuple(sorted(e for e in out if e > d))


def _rate(x: Rate, d: date) -> float:
    return float(x(d)) if callable(x) else float(x)


# ============================================================================ pricing context
@dataclass(frozen=True)
class EventVol:
    """An earnings event as seen by MODEL pricing: a leg valued before the reaction-day close whose
    expiry is on/after the reaction day gets total variance base_iv²·T + ``var`` (one-day event
    variance); valuations from the reaction day on use the base IV of ``pre_day`` (the last close
    before the reaction day) only — the IV crush, and an HV proxy not inflated by the move itself."""
    reaction: date
    pre_day: date
    var: float
    source: str = "symbol"          # symbol | universe | default (see earnings.event_variance)


@dataclass
class _Market:
    symbol: str
    bars: BarArrays
    provider: PriceProvider
    rate: Rate
    div_yield: Rate
    is_index: bool = False
    event: EventVol | None = None
    _prices: dict = field(default_factory=dict, init=False, repr=False)   # option price memo (per context)

    def close(self, d: date) -> float | None:
        i = self.bars.index_of(d)
        return None if i is None else float(self.bars.close[i])

    def expiries(self, d: date) -> tuple[Sequence[date], bool]:
        f = getattr(self.provider, "listed_expiries", None)
        real = [e for e in (f(d, self.symbol) if f else []) if e > d]
        return (sorted(real), True) if real else (synth_expiries(d), False)

    def strikes(self, d: date, expiry: date, right: str, spot: float, real_listing: bool) -> np.ndarray:
        if real_listing:
            f = getattr(self.provider, "listed_strikes", None)
            ks = sorted(f(d, self.symbol, expiry, right)) if f else []
            if ks:
                return np.asarray(ks, dtype=float)
        return synth_strikes(spot, self.is_index)

    def iv(self, d: date, expiry: date) -> float | None:
        """Model vol of a leg expiring at ``expiry`` valued at d's close (event-adjusted, see EventVol)."""
        ev = self.event
        base_day = ev.pre_day if ev is not None and d >= ev.reaction else d
        base = self.provider.iv_proxy(base_day, self.symbol, max((expiry - d).days, 1))
        if base is None or ev is None or not d < ev.reaction <= expiry:
            return base
        T = cal.year_fraction_to_expiry(expiry, close_dt(d))
        return math.sqrt(base * base + ev.var / T) if T > 0 else base

    def price_leg(self, d: date, spot: float, leg: Leg) -> tuple[float, bool] | None:
        """(price per share, is_real) at d's close, or None when unpriceable (memoized: the same
        contract is priced by many strategies on the same day)."""
        if leg.kind == "stock":
            return spot, True
        key = (d, spot, leg.expiry, leg.strike, leg.right)
        if key not in self._prices:
            self._prices[key] = self._price_option(d, spot, leg)
        return self._prices[key]

    def _price_option(self, d: date, spot: float, leg: Leg) -> tuple[float, bool] | None:
        if d >= leg.expiry:
            intr = max(spot - leg.strike, 0.0) if leg.right == "C" else max(leg.strike - spot, 0.0)
            return intr, True
        m = self.provider.real_mid(d, self.symbol, leg.expiry, leg.strike, leg.right)
        if m is not None and math.isfinite(m):
            return float(m), True
        iv = self.iv(d, leg.expiry)
        if iv is None or not iv > 0:
            return None
        T = cal.year_fraction_to_expiry(leg.expiry, close_dt(d))
        return float(bs.price(spot, leg.strike, T, _rate(self.rate, d), _rate(self.div_yield, d), iv, leg.right)), False

    def spread_ok(self, d: date, leg: Leg, max_spread: float | None) -> bool:
        f = getattr(self.provider, "real_spread_pct", None)
        if max_spread is None or f is None or leg.kind == "stock" or d >= leg.expiry:
            return True
        s = f(d, self.symbol, leg.expiry, leg.strike, leg.right)
        return s is None or s <= max_spread

    def value(self, d: date, spot: float, legs: Sequence[Leg], max_spread: float | None = None):
        """(position value $, all_real, prices) or None if any leg is unpriceable/too wide."""
        total, real, prices = 0.0, True, []
        for l in legs:
            p = self.price_leg(d, spot, l)
            if p is None:
                return None
            if p[1] and not self.spread_ok(d, l, max_spread):
                return None
            total += l.qty * l.multiplier * p[0]
            real &= p[1]
            prices.append(p[0])
        return total, real, prices


# ============================================================================ position building
def _next_monthly_after(exps: Sequence[date], front: date) -> date | None:
    return next((e for e in exps if e > front and cal.is_monthly_expiry(e)), None)


def build_legs(mk: _Market, template: Template, d: date, spot: float, front: date, back: date | None,
               rule: str, value: float | None, width_pct: float | None, real_listing: bool,
               qty: float = 1.0) -> list[Leg] | None:
    """Concrete legs (no prices) for a template on date d."""
    exps = (front, back)
    if any(l.expiry_index == 1 for l in template.legs) and back is None:
        return None

    def strikes_for(ei, right):
        return mk.strikes(d, exps[ei], right, spot, real_listing)

    def delta_for(ei, right, ks):
        iv = mk.iv(d, exps[ei])
        if iv is None:
            return np.full(len(ks), np.nan)
        T = cal.year_fraction_to_expiry(exps[ei], close_dt(d))
        return bs.delta(spot, ks, T, _rate(mk.rate, d), _rate(mk.div_yield, d), iv, right)

    if rule == "delta" and mk.iv(d, front) is None:
        return None
    ks = resolve_strikes(template, spot, strikes_for, rule, value, width_pct, delta_for)
    legs = []
    for tl, k in zip(template.legs, ks):
        if tl.kind == "stock":
            legs.append(Leg("stock", tl.qty * qty))
        else:
            if k is None:
                return None
            legs.append(Leg("option", tl.qty * qty, tl.right, k, exps[tl.expiry_index]))
    return legs


def risk_basis(legs: Sequence[Leg], prices: Sequence[float], spot: float) -> float:
    """Debit for debit positions; else max risk (see module doc)."""
    priced = [Leg(l.kind, l.qty, l.right, l.strike, l.expiry, p) for l, p in zip(legs, prices)]
    cost = net_cost(priced)
    if cost > 1e-9:
        return cost
    opts = [l for l in priced if l.kind == "option"]
    if len({l.expiry for l in opts}) <= 1:
        stock = sum(l.qty for l in priced if l.kind == "stock") / OPTION_MULT
        prof = linear_profile(np.array([[l.strike for l in opts]], float), [l.right == "C" for l in opts],
                              [l.qty for l in opts], stock, np.array([cost / OPTION_MULT]), spot,
                              np.array([0.1]), np.array([0.3]), 0.0)
        ml = float(prof["max_loss"][0]) * OPTION_MULT
        if math.isfinite(ml) and ml > 1e-9:
            return ml
    m = 0.0
    for l in priced:
        if l.qty < 0 and l.kind == "option":
            m += abs(l.qty) * OPTION_MULT * float(naked_margin(spot, l.strike, l.price, l.right == "C"))
        elif l.qty < 0:
            m += abs(l.qty) * spot
    return m if m > 0 else abs(cost)


@lru_cache(maxsize=None)
def is_short_premium(template_id: str) -> bool:
    """Net-credit template (reference BSM pricing at spot 100, σ 30%, 30d, $1 grid), or a calendar
    whose front leg is short."""
    t = TEMPLATES[template_id]
    if t.family == "calendar":
        return any(l.qty < 0 and l.expiry_index == 0 for l in t.legs)
    grid = np.arange(50.0, 151.0)
    ks = resolve_strikes(t, 100.0, lambda ei, r: grid)
    prem = sum(l.qty * bs.price(100.0, k, 30 / 365, 0.0, 0.0, 0.3, l.right)
               for l, k in zip(t.legs, ks) if l.kind == "option")
    return prem < 0


def _trade(symbol, legs, d0, d1, v0, v1, basis, real, note):
    pnl = v1 - v0
    return {"symbol": symbol, "entry_date": d0, "exit_date": d1, "legs": describe_legs(legs),
            "entry_value": v0, "exit_value": v1, "pnl": pnl, "ret": pnl / basis if basis > 0 else None,
            "pricing": "real" if real else "model", "note": note}


# ============================================================================ /backtest/options
def _entry_indices(a: BarArrays, entry: Mapping, lo: int, hi: int) -> list[int]:
    typ = entry.get("type", "every_n_days")
    if typ == "every_n_days":
        n = max(int(entry.get("n") or 21), 1)
        return list(range(lo, hi + 1, n))
    out, seen = [], set()
    for i in range(lo, hi + 1):
        d = a.dates[i]
        if typ == "monthly":
            key = (d.year, d.month)
            ok = True
        elif typ == "weekly":
            key = d.isocalendar()[:2]
            ok = d.weekday() >= int(entry.get("weekday") or 0)
        else:
            raise ValueError(f"unknown entry type {typ!r}")
        if ok and key not in seen:
            seen.add(key)
            out.append(i)
    return out


def _choose_expiry(exps: Sequence[date], d: date, target_dte: int) -> date | None:
    if not exps:
        return None
    return min(exps, key=lambda e: (abs((e - d).days - target_dte), e))


def _run_trade(mk: _Market, legs: list[Leg], i0: int, last_i: int, exit_rule: Mapping,
               max_spread: float | None = None, fixed_exit: int | None = None):
    """Simulate one trade entered at bar i0's close. Exits are checked on each later close (first
    hit: expiry, dte, stop loss, take profit) or taken at ``fixed_exit``. ``max_spread`` skips the
    observation when a real leg's spread% exceeds it (entry and fixed exit). Returns
    (trade dict, daily [(date, pnl, basis)]) or None when unpriceable."""
    a = mk.bars
    d0, s0 = a.dates[i0], float(a.close[i0])
    ent = mk.value(d0, s0, legs, max_spread)
    if ent is None:
        return None
    v0, real0, p0 = ent
    basis = risk_basis(legs, p0, s0)
    opts = [l for l in legs if l.kind == "option"]
    first_exp = min((l.expiry for l in opts), default=None)
    priced = [Leg(l.kind, l.qty, l.right, l.strike, l.expiry, p) for l, p in zip(legs, p0)]
    tp, sl = exit_rule.get("take_profit"), exit_rule.get("stop_loss")
    mp = None
    if tp is not None and fixed_exit is None and len({l.expiry for l in opts}) == 1:   # take-profit target only
        stock = sum(l.qty for l in legs if l.kind == "stock") / OPTION_MULT
        prof = linear_profile(np.array([[l.strike for l in opts]], float), [l.right == "C" for l in opts],
                              [l.qty for l in opts], stock, np.array([net_cost(priced) / OPTION_MULT]), s0,
                              np.array([0.1]), np.array([0.3]), 0.0)
        mp = float(prof["max_profit"][0]) * OPTION_MULT
        mp = mp if math.isfinite(mp) and mp > 0 else None
    if fixed_exit is not None:            # earnings windows: value only at the exit close
        d = a.dates[fixed_exit]
        cur = mk.value(d, float(a.close[fixed_exit]), legs, max_spread)
        if cur is None:
            return None
        return _trade(mk.symbol, legs, d0, d, v0, cur[0], basis, real0 and cur[1], "exit"), [(d, cur[0] - v0, basis)]
    typ = exit_rule.get("type", "expiry")
    path = []
    for j in range(i0 + 1, last_i + 1):
        d, s = a.dates[j], float(a.close[j])
        cur = mk.value(d, s, legs)
        if cur is None:
            continue
        v, real, _ = cur
        pnl = v - v0
        path.append((d, pnl, basis))
        reason = None
        if first_exp is not None and d >= first_exp:
            reason = "expiry"
        elif typ == "dte" and first_exp is not None and (first_exp - d).days <= int(exit_rule.get("dte") or 0):
            reason = "dte"
        if sl is not None and pnl <= -float(sl) * basis:
            reason = "stop loss"
        elif tp is not None and pnl >= float(tp) * (mp if mp is not None else basis):
            reason = "take profit"
        if reason:
            return _trade(mk.symbol, legs, d0, d, v0, v, basis, real0 and real, reason), path
    if not path:
        return None
    d, pnl, _ = path[-1]
    cur = mk.value(d, float(a.close[a.index_of(d)]), legs)
    return _trade(mk.symbol, legs, d0, d, v0, cur[0], basis, real0 and cur[1], "open at end"), path


def _option_equity(a: BarArrays, results, lo: int, hi: int) -> tuple[list, list, float]:
    """Equity = capital + realized + unrealized P/L, marked daily; capital = peak Σ basis of
    concurrently open trades. Benchmark = buy & hold of the underlying with the same start value."""
    if not results:
        return [], [], 0.0
    days = a.dates[lo: hi + 1]
    pos = {d: k for k, d in enumerate(days)}
    pnl = np.zeros(len(days))
    used = np.zeros(len(days))
    for trade, path in results:
        k0 = pos[trade["entry_date"]]
        k1 = pos[trade["exit_date"]]
        marks = {d: p for d, p, _ in path}
        last = 0.0
        for k in range(k0, len(days)):
            d = days[k]
            if k > k1:
                pnl[k] += trade["pnl"]
                continue
            if d in marks:
                last = marks[d]
            pnl[k] += last if k < k1 else trade["pnl"]
            if k < k1:
                used[k] += path[0][2]
    capital = float(max(used.max(), max(p[0][2] for _, p in results)))
    first = min(pos[t["entry_date"]] for t, _ in results)
    last = max(pos[t["exit_date"]] for t, _ in results)
    eq = [(days[k], capital + float(pnl[k])) for k in range(first, last + 1)]
    c0 = float(a.close[lo + first])
    bench = [(days[k], capital * float(a.close[lo + k]) / c0) for k in range(first, last + 1)]
    return eq, bench, capital


def backtest_options(symbol: str, bars: Sequence, template_id: str, *, provider: PriceProvider,
                     rate: Rate, div_yield: Rate = 0.0, strike_rule: str = "atm",
                     rule_value: float | None = None, width_pct: float | None = None,
                     entry: Mapping | None = None, target_dte: int = 30, exit: Mapping | None = None,
                     start: date | None = None, end: date | None = None, qty: float = 1.0,
                     is_index: bool = False) -> dict:
    """BacktestResult for OptionsBacktestRequest (types.ts). entry.type: every_n_days(n) |
    monthly | weekly(weekday 0=Mon) | roll (re-enter on the previous trade's exit day; sequential).
    exit: {type: expiry|dte, dte, take_profit (fraction of max profit, or of basis when unlimited),
    stop_loss (fraction of basis)}. Scheduled entries may overlap."""
    entry = dict(entry or {"type": "every_n_days", "n": 21})
    exit = dict(exit or {"type": "expiry"})
    template = TEMPLATES[template_id]
    a = bar_arrays(list(bars))
    mk = _Market(symbol, a, provider, rate, div_yield, is_index)
    notes = ["Option prices: stored EOD mids when available, else Black-Scholes with an IV proxy "
             "(stored ATM IV → IV30 → HV30·k); model rows are approximations.",
             "Not compounded: one position size throughout; equity = capital (peak capital at risk of the open "
             "trades) + cumulative P/L, so total return and drawdown are relative to that capital and fall below "
             "−100% when cumulative losses exceed it."]
    if len(a) < 2:
        return build_result([], [], None, notes)
    lo = a.index_on_or_after(start) if start else 0
    hi = (a.index_before(end + timedelta(days=1)) if end else len(a) - 1)
    if lo is None or hi is None or hi <= lo:
        return build_result([], [], None, notes)
    results = []

    def open_at(i):
        d, s = a.dates[i], float(a.close[i])
        exps, real_listing = mk.expiries(d)
        front = _choose_expiry(exps, d, target_dte)
        if front is None:
            return None
        back = _next_monthly_after(exps, front)
        legs = build_legs(mk, template, d, s, front, back, strike_rule, rule_value, width_pct, real_listing, qty)
        return None if legs is None else _run_trade(mk, legs, i, hi, exit)

    if entry.get("type") == "roll":
        i = lo
        while i < hi:
            r = open_at(i)
            if r is None:
                i += 1
                continue
            results.append(r)
            nxt = a.index_of(r[0]["exit_date"])
            if r[0]["note"] == "open at end" or nxt is None or nxt <= i:
                break
            i = nxt
    else:
        for i in _entry_indices(a, entry, lo, hi - 1):
            r = open_at(i)
            if r is not None:
                results.append(r)
    trades = [t for t, _ in results]
    eq, bench, _ = _option_equity(a, results, lo, hi)
    if any(t["pricing"] == "model" for t in trades):
        notes.append("Model-priced trades use strikes snapped to a listing-like grid and synthesized expiries "
                     "when no stored listing exists.")
    return build_result(trades, eq, bench or None, notes)


def straddle_backtest(symbol: str, bars: Sequence, *, provider: PriceProvider, rate: Rate,
                      div_yield: Rate = 0.0, target_dte: int = 30, years: float = 2.0,
                      is_index: bool = False) -> dict:
    """StraddleInfo.backtest: rolling ~target_dte long ATM straddle held to expiry over the last
    ``years`` of bars."""
    bars = list(bars)
    if not bars:
        return build_result([], [], None, [])
    last = max(b.date for b in bars)
    start = last - timedelta(days=int(round(365.25 * years)))
    return backtest_options(symbol, bars, "long-straddle", provider=provider, rate=rate, div_yield=div_yield,
                            entry={"type": "roll"}, target_dte=target_dte, exit={"type": "expiry"},
                            start=start, is_index=is_index)


def straddle_wing_backtest(symbol: str, bars: Sequence, *, provider: PriceProvider, rate: Rate,
                           div_yield: Rate = 0.0, side: str = "short", wing_pct: float = 0.05,
                           target_dte: int = 30, entry: Mapping | None = None, exit: Mapping | None = None,
                           start: date | None = None, end: date | None = None, is_index: bool = False) -> dict:
    """/backtest/straddle-wing preset: ATM straddle ± wings (iron butterfly), short or long."""
    tid = "iron-butterfly-short" if side == "short" else "iron-butterfly-long"
    return backtest_options(symbol, bars, tid, provider=provider, rate=rate, div_yield=div_yield,
                            width_pct=wing_pct, entry=entry or {"type": "roll"}, target_dte=target_dte,
                            exit=exit or {"type": "expiry"}, start=start, end=end, is_index=is_index)


# ============================================================================ earnings strategies
@dataclass(frozen=True)
class EarningsStrategy:
    id: str
    label: str
    template: str
    rule: str = "atm"
    value: float | None = None
    width_pct: float | None = None


def _es(id, label, template, rule="atm", value=None, width=None):
    return EarningsStrategy(id, label, template, rule, value, width)


EARNINGS_STRATEGIES: tuple[EarningsStrategy, ...] = (
    _es("long-straddle", "Long ATM straddle", "long-straddle"),
    _es("short-straddle", "Short ATM straddle", "short-straddle"),
    _es("long-strangle-5", "Long 5% OTM strangle", "long-strangle", "pct_otm", 0.05),
    _es("short-strangle-5", "Short 5% OTM strangle", "short-strangle", "pct_otm", 0.05),
    _es("long-strangle-10", "Long 10% OTM strangle", "long-strangle", "pct_otm", 0.10),
    _es("short-strangle-10", "Short 10% OTM strangle", "short-strangle", "pct_otm", 0.10),
    _es("long-call-atm", "Long ATM call", "long-call"),
    _es("short-call-atm", "Short ATM call", "short-call"),
    _es("long-put-atm", "Long ATM put", "long-put"),
    _es("short-put-atm", "Short ATM put", "short-put"),
    _es("long-call-25d", "Long 25Δ call", "long-call", "delta", 0.25),
    _es("short-call-25d", "Short 25Δ call", "short-call", "delta", 0.25),
    _es("long-put-25d", "Long 25Δ put", "long-put", "delta", 0.25),
    _es("short-put-25d", "Short 25Δ put", "short-put", "delta", 0.25),
    _es("bull-call-spread", "Bull call spread (ATM, 5% wide)", "bull-call-spread", "atm", None, 0.05),
    _es("bear-call-spread", "Bear call spread (ATM, 5% wide)", "bear-call-spread", "atm", None, 0.05),
    _es("bull-put-spread", "Bull put spread (ATM, 5% wide)", "bull-put-spread", "atm", None, 0.05),
    _es("bear-put-spread", "Bear put spread (ATM, 5% wide)", "bear-put-spread", "atm", None, 0.05),
    _es("iron-condor-short", "Short iron condor (5%/10%)", "iron-condor-short", "pct_otm", 0.05, 0.05),
    _es("iron-condor-long", "Long iron condor (5%/10%)", "iron-condor-long", "pct_otm", 0.05, 0.05),
    _es("iron-butterfly-short", "Short iron butterfly (ATM, 5% wings)", "iron-butterfly-short", "atm", None, 0.05),
    _es("iron-butterfly-long", "Long iron butterfly (ATM, 5% wings)", "iron-butterfly-long", "atm", None, 0.05),
    _es("calendar-call-long", "Long call calendar (ATM)", "calendar-call"),
    _es("calendar-call-short", "Short call calendar (ATM)", "calendar-call-short"),
    _es("calendar-put-long", "Long put calendar (ATM)", "calendar-put"),
    _es("calendar-put-short", "Short put calendar (ATM)", "calendar-put-short"),
    _es("covered-call-atm", "Covered call (ATM)", "covered-call"),
    _es("long-stock", "Long stock", "long-stock"),
    _es("short-stock", "Short stock", "short-stock"),
    _es("protective-put", "Stock + ATM protective put", "protective-put"),
)
EARNINGS_STRATEGY_IDS = tuple(s.id for s in EARNINGS_STRATEGIES)
MAX_REAL_SPREAD = 0.25


class EarningsWindow(NamedTuple):
    event: Any
    first: date          # earliest possible reaction session (earnings.reaction_window)
    last: date           # latest possible reaction session (= first unless timing is unknown)
    entry: date
    exit: date


def earnings_windows(bars: BarArrays, events: Sequence, lookback: int, entry: int, exit: int) -> list[EarningsWindow]:
    """Windows of the most recent ``lookback`` past events whose entry and exit closes exist, placed
    without look-ahead (earnings.reaction_window): entry n (<0) -> add_trading_days(first, n);
    exit n -> add_trading_days(last, n) (0 = close of the last possible reaction session)."""
    out = []
    for ev in sorted(events, key=lambda e: e.date, reverse=True):
        first, last = reaction_window(ev.date, ev.timing)
        d0, d1 = cal.add_trading_days(first, entry), cal.add_trading_days(last, exit)
        if d1 <= d0 or bars.index_of(d0) is None or bars.index_of(d1) is None:
            continue
        out.append(EarningsWindow(ev, first, last, d0, d1))
        if len(out) >= lookback:
            break
    return out


EVENT_VOL_NOTE = ("Model-priced legs whose expiry spans the reaction day use total variance base_iv²·T + event_var, "
                  "event_var = mean squared log reaction-day move (reaction close / pre-event close) of the symbol's "
                  "PRIOR earnings events only (≥ 4 needed; else the universe median; else 5%²). From the reaction "
                  "day on only the pre-event base IV is used (IV crush). Real-priced legs are unchanged.")


def _event_markets(mk: _Market, events: Sequence, windows: Sequence[tuple],
                   universe_event_var: float | None) -> list[_Market]:
    """One pricing context per earnings window, carrying that event's EventVol (prior events only)."""
    a = mk.bars
    moves = reaction_log_moves(a, events)
    out = []
    for w in windows:
        var, src = event_variance(moves, w.first, universe_event_var)    # events reacting before this one
        pre = a.index_before(w.first)
        out.append(replace(mk, event=EventVol(w.last, a.dates[pre] if pre is not None else w.entry, var, src)))
    return out


def _event_notes(windows: Sequence[EarningsWindow], markets: Sequence[_Market]) -> list[str]:
    srcs = [m.event.source for m in markets]
    counts = ", ".join(f"{srcs.count(k)} {k}" for k in ("symbol", "universe", "default") if srcs.count(k))
    return ([EVENT_VOL_NOTE] + ([f"Event variance source per event: {counts}."] if counts else [])
            + ([UNKNOWN_TIMING_NOTE] if any(w.first != w.last for w in windows) else []))


def _earnings_trade(mk: _Market, spec: EarningsStrategy, d0: date, d1: date):
    a = mk.bars
    i0, i1 = a.index_of(d0), a.index_of(d1)
    s0 = float(a.close[i0])
    template = TEMPLATES[spec.template]
    exps, real_listing = mk.expiries(d0)
    min_exp = d1 + timedelta(days=2) if is_short_premium(spec.template) else d1
    front = next((e for e in exps if e >= min_exp), None)
    if front is None:
        return None
    back = _next_monthly_after(exps, front)
    legs = build_legs(mk, template, d0, s0, front, back, spec.rule, spec.value, spec.width_pct, real_listing)
    if legs is None:
        return None
    r = _run_trade(mk, legs, i0, i1, {"type": "fixed"}, MAX_REAL_SPREAD, fixed_exit=i1)
    return None if r is None else r[0]


def earnings_strategy_trades(symbol: str, bars: Sequence, events: Sequence, strategy_id: str, *,
                             provider: PriceProvider, rate: Rate, div_yield: Rate = 0.0, lookback: int = 12,
                             entry: int = -1, exit: int = 1, is_index: bool = False,
                             universe_event_var: float | None = None) -> list[dict]:
    """BacktestTrade[] (newest first) for one earnings strategy. ``universe_event_var``: the caller's
    universe median event variance (fallback for symbols with < 4 prior events; see EVENT_VOL_NOTE)."""
    spec = next(s for s in EARNINGS_STRATEGIES if s.id == strategy_id)
    a = bar_arrays(list(bars))
    mk = _Market(symbol, a, provider, rate, div_yield, is_index)
    windows = earnings_windows(a, events, min(lookback, 12), entry, exit)
    out = []
    for w, m in zip(windows, _event_markets(mk, events, windows, universe_event_var)):
        t = _earnings_trade(m, spec, w.entry, w.exit)
        if t is not None:
            react = w.first.isoformat() if w.first == w.last else f"{w.first.isoformat()} or {w.last.isoformat()}"
            t["note"] = f"earnings {w.event.date.isoformat()} ({w.event.timing}), reaction {react}"
            if t["pricing"] == "model":
                t["note"] += f"; model event move σ {math.sqrt(m.event.var):.1%} ({m.event.source})"
            out.append(t)
    return out


def summarize_trades(spec_id: str, label: str, trades: Sequence[dict]) -> dict:
    """StrategyBacktestSummary."""
    rets = [t["ret"] for t in trades if t["ret"] is not None]
    pnls = [t["pnl"] for t in trades]
    return {
        "strategy": spec_id, "label": label, "trades": len(trades),
        "win_rate": float(np.mean([p > 0 for p in pnls])) if pnls else None,
        "avg_return": float(np.mean(rets)) if rets else None,
        "median_return": float(np.median(rets)) if rets else None,
        "best": float(max(rets)) if rets else None, "worst": float(min(rets)) if rets else None,
        "avg_pnl": float(np.mean(pnls)) if pnls else None,
        "real_count": sum(t["pricing"] == "real" for t in trades),
        "model_count": sum(t["pricing"] == "model" for t in trades),
    }


def earnings_strategy_backtest(symbol: str, bars: Sequence, events: Sequence, *, provider: PriceProvider,
                               rate: Rate, div_yield: Rate = 0.0, lookback: int = 12, entry: int = -1,
                               exit: int = 1, strategies: Sequence[str] | None = None,
                               is_index: bool = False, universe_event_var: float | None = None) -> dict:
    """EarningsStrategyResult: one StrategyBacktestSummary per strategy (SPEC §7) + notes on the
    model's event variance (see EVENT_VOL_NOTE; ``universe_event_var`` as in earnings_strategy_trades)."""
    sel = [s for s in EARNINGS_STRATEGIES if strategies is None or s.id in strategies]
    a = bar_arrays(list(bars))
    mk = _Market(symbol, a, provider, rate, div_yield, is_index)
    windows = earnings_windows(a, events, min(lookback, 12), entry, exit)
    markets = _event_markets(mk, events, windows, universe_event_var)
    rows = []
    for spec in sel:
        trades = [t for w, m in zip(windows, markets) if (t := _earnings_trade(m, spec, w.entry, w.exit)) is not None]
        rows.append(summarize_trades(spec.id, spec.label, trades))
    return {"symbol": symbol, "lookback": lookback, "entry": entry, "exit": exit, "rows": rows,
            "notes": _event_notes(windows, markets)}
