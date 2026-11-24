"""Valuing holdings and aggregating them (pure; docs/INTEGRATION.md §11 "Portfolio").

Per position (all money in the position's own currency):
- market value = price × qty × multiplier; cost basis = average cost × qty × multiplier;
- unrealized P/L = market value − cost basis, and in % of |cost basis|;
- day P/L = the broker's own figure when it reports one, else (price − previous close) × qty × multiplier
  (`day_pnl_basis` "prev_close": exact only for a position held through the previous close), else unknown;
- greeks of an option from its IV (Black-Scholes, as the calculator: strategies.position_greeks), position totals:
  delta (share-equivalents), delta $ (= delta × underlying price), gamma (shares per $1), theta ($ per day),
  vega ($ per vol point); a stock: delta = qty, the rest 0; unknown (None) without an IV;
- beta-weighted delta vs SPY (USD positions with a known 1-year beta): delta × underlying price × beta / SPY price,
  in SPY share-equivalents.

Aggregates (per underlying, account, broker, total) sum each currency on its own (`by_currency`), and convert into
the reporting currency only with an FX rate a broker gave (`converted`; null with `missing_fx` otherwise) — never a
silent cross-currency sum. A value unknown for some positions is summed over the others and counted in `unknown`.
Gamma is summed per underlying only (shares per $1 of different stocks do not add up)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from ..analytics.strategies import OPTION_MULT, Leg, close_dt, position_greeks
from .model import Balance, FxRate, Holding

SUMMED = ("market_value", "cost_basis", "unrealized_pnl", "day_pnl", "delta_dollars", "theta", "vega")
BALANCE_KEYS = ("net_liquidation", "cash", "buying_power")


@dataclass(frozen=True, slots=True)
class Mark:
    """What is known about a holding's market now. `price` per share; `source` "live" (live quotes) | "stored" (the
    last stored quote / chain) | "broker" (the broker's own price); `spot`: the underlying's price."""
    price: float | None = None
    source: str | None = None
    ts: datetime | None = None
    prev_close: float | None = None
    spot: float | None = None
    iv: float | None = None
    delayed: bool | None = None


def _ok(x: float | None) -> bool:
    return x is not None and math.isfinite(x)


def position_greeks_of(h: Holding, mk: Mark, now: datetime, r: float, q: float) -> dict | None:
    """{delta, gamma, theta, vega} position totals, or None when unknown (an option without IV or underlying price)."""
    if h.kind == "stock":
        return {"delta": h.qty, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    if h.kind != "option" or h.expiry is None or h.strike is None or h.right is None:
        return None
    if close_dt(h.expiry) <= now:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    if not (_ok(mk.iv) and _ok(mk.spot)) or mk.iv <= 0 or mk.spot <= 0:   # type: ignore[operator]
        return None
    g = position_greeks([Leg("option", h.qty, h.right, h.strike, h.expiry, iv=mk.iv)], mk.spot, now, r, q)  # type: ignore[arg-type]
    scale = h.multiplier / OPTION_MULT     # position_greeks counts 100 shares per contract
    return {k: v * scale for k, v in g.items()}


def position(h: Holding, mk: Mark, *, now: datetime, r: float, q: float, beta: float | None = None,
             spy_price: float | None = None) -> dict:
    """One position row (types.ts PortfolioPosition)."""
    units = h.qty * h.multiplier
    price = mk.price if _ok(mk.price) else None
    mv = price * units if price is not None else None
    cost = h.avg_cost * units if _ok(h.avg_cost) else None
    upnl = mv - cost if mv is not None and cost is not None else None
    if _ok(h.broker_day_pnl):
        day, basis = h.broker_day_pnl, "broker"
    elif price is not None and _ok(mk.prev_close):
        day, basis = (price - mk.prev_close) * units, "prev_close"   # type: ignore[operator]
    else:
        day, basis = None, None
    g = position_greeks_of(h, mk, now, r, q)
    spot = mk.spot if _ok(mk.spot) else (price if h.kind == "stock" else None)
    delta_dollars = g["delta"] * spot if g is not None and spot is not None else None
    bwd = None
    if g is not None and h.currency == "USD" and spot is not None and _ok(beta) and _ok(spy_price) and spy_price > 0:  # type: ignore[operator]
        bwd = g["delta"] * spot * beta / spy_price   # type: ignore[operator]
    return {
        "broker": h.broker, "account_id": h.account_id, "account": h.account, "kind": h.kind, "symbol": h.symbol,
        "underlying": h.underlying, "osi": h.osi, "expiry": h.expiry, "right": h.right, "strike": h.strike,
        "qty": h.qty, "multiplier": h.multiplier, "currency": h.currency, "avg_cost": h.avg_cost,
        "price": price, "price_source": mk.source if price is not None else None, "price_ts": mk.ts,
        "delayed": mk.delayed, "spot": spot, "iv": mk.iv if _ok(mk.iv) else None,
        "market_value": mv, "cost_basis": cost, "unrealized_pnl": upnl,
        "unrealized_pct": upnl / abs(cost) if upnl is not None and cost else None,
        "day_pnl": day, "day_pnl_basis": basis,
        "delta": g["delta"] if g else None, "delta_dollars": delta_dollars, "gamma": g["gamma"] if g else None,
        "theta": g["theta"] if g else None, "vega": g["vega"] if g else None,
        "beta": beta if _ok(beta) else None, "bwd_delta_spy": bwd,
    }


# ----------------------------------------------------------------------------- FX
def fx_rate(frm: str, to: str, rates: Sequence[FxRate]) -> tuple[float, list[FxRate]] | None:
    """(units of `to` per 1 `frm`, the rates used): a direct or inverse rate, else one hop through a third currency;
    the freshest rate of a pair wins. None when the rates do not connect the two."""
    if frm == to:
        return 1.0, []
    best: dict[tuple[str, str], FxRate] = {}
    for x in rates:
        if x.rate > 0 and ((x.base, x.quote) not in best or x.asof > best[(x.base, x.quote)].asof):
            best[(x.base, x.quote)] = x

    def direct(a: str, b: str) -> tuple[float, FxRate] | None:
        if (a, b) in best:
            return best[(a, b)].rate, best[(a, b)]
        if (b, a) in best:
            return 1.0 / best[(b, a)].rate, best[(b, a)]
        return None

    if (d := direct(frm, to)) is not None:
        return d[0], [d[1]]
    for mid in sorted({c for pair in best for c in pair} - {frm, to}):
        a, b = direct(frm, mid), direct(mid, to)
        if a is not None and b is not None:
            return a[0] * b[0], [a[1], b[1]]
    return None


def fx_view(x: FxRate) -> dict:
    return {"base": x.base, "quote": x.quote, "rate": x.rate, "source": x.source, "asof": x.asof,
            "label": f"1 {x.base} = {x.rate:.4f} {x.quote} ({x.source})"}


# ----------------------------------------------------------------------------- aggregation
def _sum(rows: Iterable[Mapping], keys: Sequence[str]) -> dict:
    out: dict = {k: 0.0 for k in keys}
    unknown: dict[str, int] = {}
    n = 0
    for r in rows:
        n += 1
        for k in keys:
            v = r.get(k)
            if v is None:
                unknown[k] = unknown.get(k, 0) + 1
            else:
                out[k] += v
    for k in keys:
        if unknown.get(k) == n and n:
            out[k] = None          # nothing known
    return {**out, "unknown": unknown}


def _with_pct(v: dict) -> dict:
    cost, upnl = v.get("cost_basis"), v.get("unrealized_pnl")
    return {**v, "unrealized_pct": upnl / abs(cost) if upnl is not None and cost else None}


def _convert(by_currency: Mapping[str, dict], keys: Sequence[str], to: str, rates: Sequence[FxRate]) -> dict:
    """{currency, values (converted sums) | None, missing_fx, rates (the FX rates used)}."""
    total: dict = {k: None for k in keys}
    missing, used = [], {}
    unknown: dict[str, int] = {}
    for cur, v in by_currency.items():
        fx = fx_rate(cur, to, rates)
        if fx is None:
            missing.append(cur)
            continue
        k_rate, rs = fx
        for x in rs:
            used[(x.base, x.quote)] = x
        for k in keys:
            if v.get(k) is not None:
                total[k] = (total[k] or 0.0) + v[k] * k_rate
        for k, c in v.get("unknown", {}).items():
            unknown[k] = unknown.get(k, 0) + c
    values = None if missing else {**total, "unknown": unknown}
    return {"currency": to, "values": values, "missing_fx": sorted(missing), "rates": [fx_view(x) for x in used.values()]}


def aggregate(rows: Sequence[Mapping], currency: str, rates: Sequence[FxRate], keys: Sequence[str] = SUMMED) -> dict:
    """{by_currency: {CUR: sums}, converted: {currency, values | None, missing_fx, rates}, positions} (see module
    doc); `unrealized_pct` follows the sums."""
    by: dict[str, list[Mapping]] = {}
    for r in rows:
        by.setdefault(r["currency"], []).append(r)
    by_currency = {c: _sum(rs, keys) for c, rs in sorted(by.items())}
    conv = _convert(by_currency, keys, currency, rates)
    if conv["values"] is not None and "cost_basis" in keys:
        conv["values"] = _with_pct(conv["values"])
    if "cost_basis" in keys:
        by_currency = {c: _with_pct(v) for c, v in by_currency.items()}
    return {"by_currency": by_currency, "converted": conv, "positions": len(rows)}


def balances(accounts_balances: Iterable[Balance], currency: str, rates: Sequence[FxRate]) -> dict:
    """Account balances (net liquidation, cash, buying power) summed per currency and converted like positions."""
    rows = [{"currency": b.currency, "net_liquidation": b.net_liquidation, "cash": b.cash,
             "buying_power": b.buying_power} for b in accounts_balances]
    return aggregate(rows, currency, rates, BALANCE_KEYS)


def beta_weighted(rows: Sequence[Mapping], spy_price: float | None) -> dict:
    """Portfolio delta weighted to SPY: {spy_shares, dollars (× SPY price), positions (counted), excluded (no beta,
    no delta or not USD)}."""
    known = [r["bwd_delta_spy"] for r in rows if r.get("bwd_delta_spy") is not None]
    shares = sum(known) if known else None
    return {"spy_shares": shares, "dollars": shares * spy_price if shares is not None and _ok(spy_price) else None,
            "spy_price": spy_price if _ok(spy_price) else None, "positions": len(known),
            "excluded": len(rows) - len(known)}
