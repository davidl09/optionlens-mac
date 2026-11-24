"""Risk graph of one underlying (pure): every held position on it — across accounts and brokers — as one calculator
position, each leg at its cost basis (the average cost is the leg's entry price), valued by the calculator engine
(analytics.payoff): P/L at the nearest expiry and today across underlying prices, breakevens, position greeks.

A leg needs an IV unless it has expired (the payoff then uses intrinsic value); legs without one are left out and
named in `skipped`, as are holdings that are not stock or standard options."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from ..analytics import payoff
from ..analytics.strategies import OPTION_MULT, Leg, close_dt
from .model import Holding

MAX_LEGS = 60


def legs(holdings: Sequence[Holding], ivs: Mapping[str, float | None], now: datetime) -> tuple[list[Leg], list[dict]]:
    """(calculator legs, skipped [{symbol, account, reason}]). `ivs`: OSI -> IV. Options keep the calculator's unit
    (contracts of OPTION_MULT shares): a non-standard multiplier is scaled into it."""
    out: list[Leg] = []
    skipped: list[dict] = []
    for h in holdings:
        if h.avg_cost is None:
            skipped.append({"symbol": h.symbol, "account": h.account, "reason": "no average cost"})
        elif h.kind == "stock":
            out.append(Leg("stock", h.qty, price=h.avg_cost))
        elif h.kind == "option" and h.expiry and h.strike and h.right:
            iv = ivs.get(h.osi or "")
            if iv is None and close_dt(h.expiry) > now:
                skipped.append({"symbol": h.symbol, "account": h.account, "reason": "no IV for this contract"})
                continue
            out.append(Leg("option", h.qty * h.multiplier / OPTION_MULT, h.right, h.strike, h.expiry, h.avg_cost, iv))
        else:
            skipped.append({"symbol": h.symbol, "account": h.account, "reason": "not a stock or listed option"})
    if len(out) > MAX_LEGS:
        skipped += [{"symbol": "", "account": "", "reason": f"more than {MAX_LEGS} legs: the rest left out"}]
        out = out[:MAX_LEGS]
    return out, skipped


def graph(holdings: Sequence[Holding], ivs: Mapping[str, float | None], spot: float, now: datetime, r: float,
          q: float, hv30: float | None = None, points: int = 121) -> dict:
    """{legs, skipped, ...CalcResult} of the underlying's positions; `legs` empty -> no curves (calculate is not
    run)."""
    ls, skipped = legs(holdings, ivs, now)
    if not ls:
        return {"legs": [], "skipped": skipped}
    out = payoff.calculate(ls, spot, now, r, q, hv30=hv30, points=points)
    return {**out, "skipped": skipped}
