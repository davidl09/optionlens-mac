"""Strategy calculator (CalcRequest -> CalcResult) and strategy templates.

Earnings: when the chain's next earnings reaction session (ChainArrays.next_reaction, else the event date) falls
inside the position's life, the event variance is estimated from the chain's ATM term structure
(analytics.event_vol) and, unless the request sets earnings_crush=false, legs are valued through the IV crush
(event_vol.CrushVol). CalcResult.event reports the estimate and whether it was applied."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime

from ..analytics import bs, chainmetrics as cm, event_vol, payoff, strategies
from ..analytics.calendar import now_ny
from ..analytics.chain import ChainArrays
from ..models import normalize_symbol
from . import context as ctx
from .errors import BadRequest, NoChain

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


def _earnings(ca: ChainArrays, legs: list[strategies.Leg], now: datetime
              ) -> tuple[date | None, event_vol.EventVol | None, str | None]:
    """(reaction session, estimate, why no estimate) of the next earnings when it falls inside the life of an option
    leg (now < reaction open <= the last option expiry), else (None, None, None)."""
    reaction = ca.next_reaction or ca.next_earnings
    last = max((l.expiry for l in legs if l.kind == "option"), default=None)
    if last is None or not event_vol.event_ahead(reaction, now, last):
        return None, None, None
    est, why = event_vol.estimate(ctx.derived(ca, "term_rows", cm.expiry_term_rows), reaction)
    return reaction, est, why


def _event_block(ca: ChainArrays, reaction: date, est: event_vol.EventVol | None, reason: str | None) -> dict:
    """CalcResult.event: the earnings estimate and whether the crush was applied (reason when not)."""
    return {"earnings_date": ca.next_earnings, "reaction_date": reaction,
            "implied_move_pct": est.move if est else None, "base_iv": est.base_iv if est else None,
            "method": est.method if est else None, "expiries_used": list(est.expiries) if est else None,
            "applied": reason is None, "reason": reason}


def calculate(req: dict) -> dict:
    """CalcResult for a CalcRequest dict (dates already parsed)."""
    sym = normalize_symbol(req["symbol"])
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False) or ctx.load_chain_arrays(sym)
    if ca is None:
        raise NoChain(sym, ctx.optionable(sym))
    if req.get("spot") is not None:          # the renderer's live underlying price
        ca = replace(ca, spot=float(req["spot"]))
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
    now = now_ny()
    reaction, est, why = _earnings(ca, legs, now)
    crush = req.get("earnings_crush") is not False
    vol = event_vol.CrushVol(legs, est, now) if est is not None and crush else None
    out = payoff.calculate(legs, ca.spot, now, ca.r, ca.q, hv30=ca.hv30, iv_shift=shift,
                           eval_date=req.get("eval_date"), price_range=tuple(rng) if rng else None,
                           points=points, asof=ca.asof, vol=vol, reaction=reaction)
    if reaction is not None:
        off = None if crush else "switched off (earnings_crush=false)"
        out["event"] = _event_block(ca, reaction, est, why if est is None else off)
    else:
        out["event"] = None
    for leg, d in zip(legs, out["legs"]):
        sp = vol.split(leg) if vol is not None else None
        d["iv_base"] = sp.base if sp else None
        d["base_floored"] = bool(sp and sp.floored)
    index = ctx.derived(ca, "leg_index", _index)
    for raw, leg in zip(req["legs"], out["legs"]):     # the contract's OSI: live quotes of the leg (renderer)
        i = index.get((raw.get("expiry"), float(raw["strike"]), raw.get("right"))) if raw["kind"] == "option" else None
        leg["osi"] = str(ca.osi[i]) if i is not None and ca.osi[i] is not None else None
    return out
