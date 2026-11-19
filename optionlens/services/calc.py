"""Strategy calculator (CalcRequest -> CalcResult) and strategy templates."""
from __future__ import annotations

import math
from datetime import date

from ..analytics import bs, payoff, strategies
from ..analytics.calendar import now_ny
from ..analytics.chain import ChainArrays
from ..models import normalize_symbol
from . import context as ctx
from .errors import BadRequest, NotFound

MAX_LEGS = 8
POINTS = (11, 1001)
MAX_IV_SHIFT = 0.5


def templates() -> list[dict]:
    return strategies.strategy_templates()


def _index(ca: ChainArrays) -> dict[tuple[date, float, str], int]:
    return {(ca.expiries[e], float(k), "C" if c else "P"): i
            for i, (e, k, c) in enumerate(zip(ca.exp_idx, ca.strike, ca.is_call))}


def _finite(x: float) -> float | None:
    return float(x) if x is not None and math.isfinite(x) else None


def _option_leg(ca: ChainArrays, leg: dict) -> strategies.Leg:
    right, strike, expiry = leg.get("right"), leg.get("strike"), leg.get("expiry")
    if right not in ("C", "P") or strike is None or expiry is None:
        raise BadRequest("option legs need right (C|P), strike and expiry")
    i = ctx.derived(ca, "leg_index", _index).get((expiry, float(strike), right))
    if i is None:
        raise BadRequest(f"{ca.symbol} {expiry} {right} {strike:g} is not a listed contract")
    mid = _finite(ca.mid[i])
    price = leg.get("price") if leg.get("price") is not None else mid
    if price is None:
        raise BadRequest(f"no price for {right} {strike:g} {expiry}: pass price")
    iv = leg.get("iv") if leg.get("iv") is not None else _finite(ca.iv[i])
    if iv is None and ca.t[i] > 0 and mid is not None:
        iv = bs.implied_vol(mid, ca.spot, float(strike), float(ca.t[i]), ca.r, ca.q, right)
    if iv is None and ca.dte[i] >= 0:
        raise BadRequest(f"no IV for {right} {strike:g} {expiry}: pass iv")
    return strategies.Leg("option", float(leg["qty"]), right, float(strike), expiry, float(price), iv, mid)


def _legs(ca: ChainArrays, raw: list[dict]) -> list[strategies.Leg]:
    if not 1 <= len(raw) <= MAX_LEGS:
        raise BadRequest(f"1..{MAX_LEGS} legs required")
    out = []
    for leg in raw:
        if not leg.get("qty"):
            raise BadRequest("leg qty must be non-zero")
        if leg["kind"] == "stock":
            price = leg.get("price") if leg.get("price") is not None else ca.spot
            out.append(strategies.Leg("stock", float(leg["qty"]), price=float(price), mid=ca.spot))
        else:
            out.append(_option_leg(ca, leg))
    return out


def calculate(req: dict) -> dict:
    """CalcResult for a CalcRequest dict (dates already parsed)."""
    sym = normalize_symbol(req["symbol"])
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False) or ctx.load_chain_arrays(sym)
    if ca is None:
        raise NotFound(f"no option chain for {sym}")
    legs = _legs(ca, req["legs"])
    shift = float(req.get("iv_shift") or 0.0)
    if abs(shift) > MAX_IV_SHIFT:
        raise BadRequest(f"iv_shift must be within ±{MAX_IV_SHIFT}")
    points = int(req.get("points") or 121)
    if not POINTS[0] <= points <= POINTS[1]:
        raise BadRequest(f"points must be {POINTS[0]}..{POINTS[1]}")
    rng = req.get("price_range")
    if rng is not None and not (0 < rng[0] < rng[1]):
        raise BadRequest("price_range must be [low, high] with 0 < low < high")
    return payoff.calculate(legs, ca.spot, now_ny(), ca.r, ca.q, hv30=ca.hv30, iv_shift=shift,
                            eval_date=req.get("eval_date"), price_range=tuple(rng) if rng else None,
                            points=points, asof=ca.asof)
