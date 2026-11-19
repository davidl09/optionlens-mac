"""Per-symbol option payloads (types.ts): Chain, Activity, OpenInterest, ContractHistory,
ImpliedMoves, StraddleInfo, TradeIdeas."""
from __future__ import annotations

import re
from datetime import date

import numpy as np

from ..analytics import chainmetrics as cm
from ..analytics import optbacktest, stats, strategies
from ..analytics.chain import ChainArrays, option_row
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from . import context as ctx
from .symbol_earnings import next_reaction_date
from .errors import BadRequest, NotFound

ALL = "all"
OSI_RE = re.compile(r"^([A-Z0-9.]+?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def require_chain(symbol: str, ensure_fresh: bool = True) -> ChainArrays:
    """The symbol's ChainArrays or NotFound (symbol known but no listed options stored)."""
    sym = ctx.refresh(symbol)["symbol"] if ensure_fresh else symbol
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False)
    if ca is None:
        raise NotFound(f"no option chain for {sym}")
    return ca


def parse_expiry(ca: ChainArrays, raw: str | None, default: date | None, allow_all: bool = False) -> date | None:
    """Expiry query param -> listed expiry (``default`` when omitted, None for 'all')."""
    if raw is None or raw == "":
        return default
    if allow_all and raw == ALL:
        return None
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        raise BadRequest(f"expiry must be YYYY-MM-DD{' or all' if allow_all else ''}") from None
    if d not in ca.expiries:
        raise BadRequest(f"{d} is not a listed expiry for {ca.symbol}")
    return d


def nearest_expiry(ca: ChainArrays) -> date:
    """First expiry on/after the chain's as-of date."""
    return next((e for e in ca.expiries if e >= ca.asof_date), ca.expiries[-1])


def expiry_infos(ca: ChainArrays) -> list[dict]:
    return ctx.derived(ca, "expiry_infos", cm.expiry_infos)


def _prior_oi(ca: ChainArrays) -> tuple[date | None, dict[str, int] | None]:
    """(the symbol's own latest EOD date before the chain, its {osi: oi}); (None, None) without one,
    so no OI change is shown (never the full OI as 'change')."""
    d = options_repo.prior_eod_date(ca.asof_date, ca.symbol)
    return (d, options_repo.prior_oi(ca.asof_date, ca.symbol)) if d else (None, None)


# ----------------------------------------------------------------------------- chain
def chain(symbol: str, expiry: str | None = None) -> dict:
    ca = require_chain(symbol)
    exp = parse_expiry(ca, expiry, nearest_expiry(ca))
    sl = ca.expiry_slices()[ca.expiries.index(exp)]
    rows = [option_row(c, ca.spot) for c in ca.src[sl]]
    return {
        "symbol": ca.symbol, "underlying": ctx.quote_dict(ca.symbol), "asof": ca.asof,
        "expiries": expiry_infos(ca), "expiry": exp,
        "strikes": sorted({r["strike"] for r in rows}),
        "calls": [r for r in rows if r["right"] == "C"], "puts": [r for r in rows if r["right"] == "P"],
    }


# ----------------------------------------------------------------------------- activity / OI
def _flow_history(symbol: str) -> list[dict]:
    h = metrics_repo.history(symbol, cols=["call_volume", "put_volume", "call_oi", "put_oi"])
    keys = ("call_volume", "put_volume", "call_oi", "put_oi")
    return [{"date": ctx.as_date(d), **{k: ctx.num(h[k][i]) for k in keys}} for i, d in enumerate(h["date"])]


def activity(symbol: str, expiry: str | None = None) -> dict:
    ca = require_chain(symbol)
    exp = parse_expiry(ca, expiry, None, allow_all=True)
    _, prior = _prior_oi(ca)
    t = cm.flow_totals(ca)
    return {
        "symbol": ca.symbol, "asof": ca.asof, "expiry": exp,
        "by_expiry": expiry_infos(ca),
        "by_strike": cm.by_strike(ca, exp, prior),
        "top_contracts": cm.top_contracts(ca, 20),
        "totals": {"call_volume": t["call_volume"], "put_volume": t["put_volume"], "call_oi": t["call_oi"],
                   "put_oi": t["put_oi"],
                   "pc_volume": t["put_volume"] / t["call_volume"] if t["call_volume"] else None,
                   "pc_oi": t["put_oi"] / t["call_oi"] if t["call_oi"] else None,
                   "call_premium": t["call_premium"], "put_premium": t["put_premium"]},
        "moneyness": {f"{s}_{b}": t[f"{s}_vol_{b}"] for s in ("call", "put") for b in ("otm", "atm", "itm")},
        "history": _flow_history(ca.symbol),
    }


def open_interest(symbol: str, expiry: str | None = None) -> dict:
    ca = require_chain(symbol)
    exp = parse_expiry(ca, expiry, nearest_expiry(ca), allow_all=True)
    prior_date, prior = _prior_oi(ca)
    changes = cm.oi_changes(ca, prior, 20, exp) if prior is not None else {"top_increases": [], "top_decreases": []}
    return {
        "symbol": ca.symbol, "expiry": exp, "prior_date": prior_date,
        "expiries": expiry_infos(ca),
        "by_strike": cm.by_strike(ca, exp, prior),
        "max_pain": ctx.derived(ca, "max_pain", cm.max_pain_all),
        **changes,
    }


def parse_osi(osi: str) -> tuple[str, date, str, float]:
    """OSI code -> (root, expiry, right, strike); BadRequest when malformed."""
    m = OSI_RE.match(osi.upper())
    if not m:
        raise BadRequest(f"malformed OSI code {osi!r}")
    root, yy, mm, dd, right, k = m.groups()
    try:
        exp = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        raise BadRequest(f"malformed OSI code {osi!r}") from None
    return root, exp, right, int(k) / 1000.0


def contract(symbol: str, osi: str) -> dict:
    osi = osi.upper()
    _, exp, right, strike = parse_osi(osi)
    sym = ctx.refresh(symbol)["symbol"]
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False)
    latest = None
    if ca is not None:
        hit = np.nonzero(ca.osi == osi)[0]
        latest = option_row(ca.src[hit[0]], ca.spot) if len(hit) else None
    h = options_repo.eod_for_osi(osi)
    if latest is None and not len(h["date"]):
        raise NotFound(f"no data for contract {osi}")
    bid, ask, last = h["bid"], h["ask"], h["last"]
    both = (bid > 0) & (ask > 0)
    mid = np.where(both, (bid + ask) / 2.0, last)
    history = [{"date": ctx.as_date(d), "bid": ctx.num(bid[i]), "ask": ctx.num(ask[i]), "mid": ctx.num(mid[i]),
                "iv": ctx.num(h["iv"][i]) if (h["iv"][i] or 0) > 0 else None,
                "volume": int(ctx.num(h["volume"][i]) or 0), "oi": int(ctx.num(h["oi"][i]) or 0),
                "delta": ctx.num(h["delta"][i]), "underlying": ctx.num(h["underlying"][i])}
               for i, d in enumerate(h["date"])]
    return {"osi": osi, "symbol": sym, "expiry": exp, "strike": strike, "right": right,
            "latest": latest, "history": history}


# ----------------------------------------------------------------------------- moves / straddle / ideas
def implied_moves(symbol: str) -> dict:
    ca = require_chain(symbol)
    return {"symbol": ca.symbol, "spot": ca.spot, "asof": ca.asof,
            "rows": cm.implied_moves(ca, next_reaction_date(ca.symbol))}


STRADDLE_BACKTEST_YEARS = 2.0


def straddle(symbol: str) -> dict:
    ca = require_chain(symbol)
    row = ctx.ensure_known(ca.symbol)
    bars_ = ctx.load_bars(ca.symbol, live=False)
    bt = optbacktest.straddle_backtest(ca.symbol, bars_, provider=ctx.DbPriceProvider(), rate=ctx.rate_fn(),
                                       div_yield=ca.q, years=STRADDLE_BACKTEST_YEARS,
                                       is_index=row["kind"] == "index") if bars_ else None
    return {"symbol": ca.symbol, "spot": ca.spot, "rows": cm.straddle_rows(ca), "backtest": bt}


# tradeable legs only; anchor |delta| 0.10–0.90 keeps out lottery tickets and deep-ITM calendars whose
# tiny, noisy debits make EV/risk meaningless
IDEA_PARAMS = strategies.ScreenParams(min_oi=50, max_spread_pct=0.20, delta_min=0.10, delta_max=0.90)


def _ideas(ca: ChainArrays) -> dict:
    bars_ = ctx.load_bars(ca.symbol, live=False)
    season = stats.seasonality(bars_, ctx.today())
    month = next((m for m in season["by_month"] if m["month"] == season["current_month"]), None)
    return strategies.rank_trade_ideas(ca, IDEA_PARAMS, n=10, seasonality=month)


def ideas(symbol: str) -> dict:
    ca = require_chain(symbol, ensure_fresh=False)
    return ctx.derived(ca, "ideas", _ideas)
