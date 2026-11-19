"""Volatility reports (SPEC §7 "Volatility"): term structure, skew, IV−HV premium, 0DTE straddles, and
the /api/compare/straddle table."""
from __future__ import annotations

import numpy as np

from ...analytics import chainmetrics
from .. import context
from . import columns as C
from .base import Col, Output, Param, ReportDef, RunParams, group, pick
from .common import KIND, SCOPE, base_mask, frame, frame_output, ge, p_int

MIN_OPT_VOL = p_int("min_option_volume", "Min option volume", 500, 0)

TERM_COLS = (*C.DESC_SHORT, *pick(C.QUOTE, "price", "change_pct"),
             *group(C.G_VOL, (Col("atm_iv_front", "Front ATM IV", "vol", default_hidden=True), Col("iv30", "IV30", "vol"),
                              Col("iv60", "IV60", "vol"), Col("iv90", "IV90", "vol"), Col("iv120", "IV120", "vol", default_hidden=True),
                              Col("iv180", "IV180", "vol"), Col("iv360", "IV360", "vol", default_hidden=True),
                              Col("term_slope", "Slope (IV90 − IV30)", "vol"), Col("term_ratio", "IV30 / IV90", "ratio"),
                              Col("backwardation", "Backwardation", "bool", description="IV30 > IV90 × 1.05"),
                              Col("iv_rank", "IV Rank", "pct"))),
             Col("option_volume", "Option Volume", "int", C.G_OPT), *C.CATALYSTS[:2])


def run_term(p: RunParams) -> Output:
    f = frame()
    m = base_mask(f, p) & np.isfinite(f["iv30"]) & np.isfinite(f["iv90"]) & ge(f["option_volume"], p["min_option_volume"])
    if p["backwardation_only"]:
        m &= f["backwardation"]
    return frame_output(f, m, TERM_COLS)


TERM = ReportDef(
    "term-structure-screener", "Term Structure Screener", "Volatility",
    "Constant-maturity IV30/60/90/180, slope and backwardation flag (IV30 > IV90 × 1.05).",
    (Param("backwardation_only", "Backwardation only", "bool", False), MIN_OPT_VOL, KIND, *SCOPE),
    TERM_COLS, run_term, ("term_ratio", "desc"),
    ("Constant-maturity IVs interpolate ATM IV in total variance between listed expiries.",))

SKEW_COLS = (*C.DESC_SHORT, *pick(C.QUOTE, "price", "change_pct"),
             *group(C.G_VOL, (Col("skew25_30", "25Δ skew (put − call)", "vol"),
                              Col("put_skew_30", "Put skew (put25 − ATM)", "vol"),
                              Col("skew_rank", "Skew rank", "pct", description="Rank of today's 25Δ skew within its stored "
                                  "history (needs ≥ 20 days)"),
                              Col("iv30", "IV30", "vol"), Col("iv_rank", "IV Rank", "pct"))),
             Col("option_volume", "Option Volume", "int", C.G_OPT), *C.CATALYSTS[:2])


def run_skew(p: RunParams) -> Output:
    f = frame()
    m = base_mask(f, p) & np.isfinite(f["skew25_30"]) & ge(f["option_volume"], p["min_option_volume"])
    return frame_output(f, m, SKEW_COLS)


SKEW = ReportDef(
    "skew-screener", "Skew Screener", "Volatility", "30-day 25-delta skew, put skew and its historic rank.",
    (MIN_OPT_VOL, KIND, *SCOPE), SKEW_COLS, run_skew, ("skew25_30", "desc"),
    ("Skew uses feed deltas, interpolated to 30 days in variance.", "Skew rank needs ≥ 20 stored days."))

PREM_COLS = (*C.DESC_SHORT, *pick(C.QUOTE, "price", "change_pct"),
             *group(C.G_VOL, (Col("iv30", "IV30", "vol"), Col("hv20", "HV20", "vol"), Col("hv30", "HV30", "vol"),
                              Col("iv_hv20_spread", "IV30 − HV20", "vol"), Col("iv_hv30_spread", "IV30 − HV30", "vol"),
                              Col("iv_hv_ratio", "IV30 / HV20", "ratio"), Col("iv_hv30_ratio", "IV30 / HV30", "ratio"),
                              Col("iv_hv_rank", "Premium rank", "pct", description="Rank of IV30 − HV30 within its stored "
                                  "history (needs ≥ 20 days)"),
                              Col("iv_rank", "IV Rank", "pct"))),
             Col("option_volume", "Option Volume", "int", C.G_OPT), *C.CATALYSTS[:2])


def run_premium(p: RunParams) -> Output:
    f = frame()
    m = base_mask(f, p) & np.isfinite(f["iv_hv30_spread"]) & ge(f["option_volume"], p["min_option_volume"])
    return frame_output(f, m, PREM_COLS)


PREMIUM = ReportDef(
    "iv-hv-premium", "IV vs HV Risk Premium", "Volatility", "Implied minus realized volatility (HV20/HV30), ratio and rank.",
    (MIN_OPT_VOL, KIND, *SCOPE), PREM_COLS, run_premium, ("iv_hv30_spread", "desc"),
    ("HV = close-to-close log-return stdev × √252.", "Premium rank needs ≥ 20 stored days."))

# ============================================================================ straddles
STRADDLE_COLS = (Col("symbol", "Symbol", "symbol"), Col("name", "Name", "text"), Col("spot", "Spot", "money"),
                 Col("expiry", "Expiry", "date"), Col("dte", "DTE", "int"), Col("strike", "Strike", "money"),
                 Col("call_mid", "Call mid", "money"), Col("put_mid", "Put mid", "money"),
                 Col("straddle", "Straddle", "money"), Col("straddle_pct", "Straddle %", "pct", description="Implied move to expiry"),
                 Col("lower_be", "Lower breakeven", "money"), Col("upper_be", "Upper breakeven", "money"),
                 Col("iv", "ATM IV", "vol"), Col("iv_move_pct", "1σ IV move", "pct", description="ATM IV · √t"))
CORE_0DTE = ("SPX", "XSP", "SPY", "QQQ", "IWM")


def _straddle_row(ca, e: int, names: dict) -> dict:
    s = chainmetrics.straddle_rows(ca)[e]
    im = chainmetrics.implied_moves(ca)[e]
    ex = chainmetrics.expiry_infos(ca)[e]
    return {"symbol": ca.symbol, "name": names.get(ca.symbol), "spot": ca.spot, **s, "iv": im["iv"],
            "iv_move_pct": im["iv_move_pct"], "call_volume": ex["call_volume"], "put_volume": ex["put_volume"]}


def _names() -> dict:
    f = frame()
    return dict(zip(f["symbol"], f["name"]))


ZDTE_COLS = (*STRADDLE_COLS, Col("call_volume", "Call vol (expiry)", "int"), Col("put_volume", "Put vol (expiry)", "int"))


def run_zero_dte(p: RunParams) -> Output:
    names = _names()
    chains = {ca.symbol: ca for ca in context.load_universe_chain_arrays(p.scope, None, 0, 0)}
    if not p["include_all"] and p.scope is None:
        chains = {s: c for s, c in chains.items() if s in CORE_0DTE}
    missing = [s for s in (p.scope or CORE_0DTE) if s not in chains]
    notes = []
    if missing:
        for ca in context.load_universe_chain_arrays(missing, None, 0, 7):
            chains[ca.symbol] = ca
        notes.append(f"No expiry today for {', '.join(missing)}: their nearest expiry is shown.")
    rows = [_straddle_row(ca, 0, names) for ca in chains.values() if len(ca)]
    return Output(rows, notes, asof=max((c.asof for c in chains.values()), default=None))


ZERO_DTE = ReportDef(
    "zero-dte-straddles", "0DTE Straddles", "Volatility",
    "At-the-money straddles expiring today for SPX, XSP, SPY, QQQ, IWM and any symbol with an expiry today.",
    (Param("include_all", "Include every symbol with an expiry today", "bool", True), *SCOPE),
    ZDTE_COLS, run_zero_dte, ("straddle_pct", "desc"),
    ("Straddle at the listed strike nearest spot, mid prices.",))

COMPARE_COLS = (*STRADDLE_COLS, Col("iv30", "IV30", "vol", C.G_VOL), Col("hv20", "HV20", "vol", C.G_VOL),
                Col("iv_rank", "IV Rank", "pct", C.G_VOL), Col("earnings_in_window", "Earnings before expiry", "bool", C.G_CAT),
                Col("next_earnings", "Next earnings", "date", C.G_CAT))


def run_compare_straddle(p: RunParams) -> Output:
    f = frame()
    names = dict(zip(f["symbol"], f["name"]))
    pos = f.index()
    rows = []
    for sym in p.scope or []:
        ca = context.load_chain_arrays(sym)
        if ca is None or not len(ca):
            rows.append({"symbol": sym, "name": names.get(sym)})
            continue
        dtes = ca.exp_dte
        e = int(np.argmin(np.abs(dtes - p["dte"])))
        r = _straddle_row(ca, e, names)
        ne = ca.next_earnings
        i = pos.get(sym)
        r.update(iv30=_fv(f["iv30"][i]) if i is not None else None, hv20=_fv(f["hv20"][i]) if i is not None else None,
                 iv_rank=ca.iv_rank, next_earnings=ne,
                 earnings_in_window=bool(ca.earnings_before_expiry()[e]))
        rows.append(r)
    return Output(rows, asof=f.asof)


def _fv(x) -> float | None:
    return float(x) if x is not None and np.isfinite(x) else None


COMPARE_STRADDLE = ReportDef(
    "compare-straddle", "Straddle Comparison", "Volatility",
    "ATM straddle (implied move) of the expiry nearest the target DTE for each symbol.",
    (Param("symbols", "Symbols", "symbols", None), p_int("dte", "Target DTE", 30, 0, 900)),
    COMPARE_COLS, run_compare_straddle, None, ("Straddle at the listed strike nearest spot, mid prices.",))

REPORTS = (TERM, SKEW, PREMIUM, ZERO_DTE)
