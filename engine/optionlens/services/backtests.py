"""Stock rule backtests and option strategy backtests (SPEC §9) over stored data."""
from __future__ import annotations

from ..analytics import backtest as stock_bt
from ..analytics import optbacktest, strategies
from ..repo import earnings as earn_repo
from . import context as ctx
from .errors import BadRequest

MAX_SYMBOLS = 50
ENTRY_TYPES = {"every_n_days", "day_of_month", "month_of_year", "days_before_earnings", "days_after_earnings",
               "gap", "rsi", "sma_cross", "new_52w"}
OPTION_ENTRY_TYPES = {"every_n_days", "monthly", "weekly"}
STRIKE_RULES = {"atm", "delta", "pct_otm"}
MAX_TARGET_DTE = 400


def stock(req: dict) -> dict:
    """BacktestResult for a StockBacktestRequest dict (dates parsed)."""
    syms = list(dict.fromkeys(req["symbols"]))
    if not 1 <= len(syms) <= MAX_SYMBOLS:
        raise BadRequest(f"1..{MAX_SYMBOLS} symbols required")
    if req["entry"].get("type") not in ENTRY_TYPES:
        raise BadRequest(f"entry.type must be one of {sorted(ENTRY_TYPES)}")
    if req["side"] not in ("long", "short"):
        raise BadRequest("side must be long or short")
    rows = [ctx.ensure_known(s) for s in syms]
    names = [r["symbol"] for r in rows]
    bars = {s: ctx.load_bars(s, live=False) for s in names}
    earnings = {s: earn_repo.for_symbol(s) for s in names}
    exit_rule = {k: v for k, v in req["exit"].items() if v is not None}
    try:
        return stock_bt.backtest_stock(bars, {k: v for k, v in req["entry"].items() if v is not None}, exit_rule,
                                       req["side"], req.get("start"), req.get("end"), earnings)
    except ValueError as e:
        raise BadRequest(str(e)) from None


def options(req: dict) -> dict:
    """BacktestResult for an OptionsBacktestRequest dict (dates parsed)."""
    if req["strategy"] not in strategies.TEMPLATES:
        raise BadRequest(f"unknown strategy template {req['strategy']!r}")
    rule = req["strike_rule"]
    if rule.get("type") not in STRIKE_RULES:
        raise BadRequest(f"strike_rule.type must be one of {sorted(STRIKE_RULES)}")
    if rule["type"] in ("delta", "pct_otm") and rule.get("value") is None:
        raise BadRequest(f"strike_rule {rule['type']} needs a value")
    width = req.get("width_pct")
    if width is not None and not 0 < width <= 1:
        raise BadRequest("width_pct is a fraction of spot (0 < width_pct <= 1, e.g. 0.05 = 5%)")
    if req["entry"].get("type") not in OPTION_ENTRY_TYPES:
        raise BadRequest(f"entry.type must be one of {sorted(OPTION_ENTRY_TYPES)}")
    if not 1 <= req["target_dte"] <= MAX_TARGET_DTE:
        raise BadRequest(f"target_dte must be 1..{MAX_TARGET_DTE}")
    if req["exit"].get("type") not in ("expiry", "dte"):
        raise BadRequest("exit.type must be expiry or dte")
    row = ctx.ensure_known(req["symbol"])
    sym = row["symbol"]
    bars = ctx.load_bars(sym, live=False)
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False)
    q = ca.q if ca is not None else ctx.dividend_yield(sym, bars[-1].close if bars else None)
    try:
        return optbacktest.backtest_options(
            sym, bars, req["strategy"], provider=ctx.DbPriceProvider(), rate=ctx.rate_fn_for(sym), div_yield=q,
            strike_rule=rule["type"], rule_value=rule.get("value"), width_pct=width,
            entry={k: v for k, v in req["entry"].items() if v is not None}, target_dte=req["target_dte"],
            exit={k: v for k, v in req["exit"].items() if v is not None}, start=req.get("start"),
            end=req.get("end"), qty=float(req.get("qty") or 1), is_index=row["kind"] == "index")
    except ValueError as e:
        raise BadRequest(str(e)) from None
