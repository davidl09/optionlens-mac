"""Dividend reports (SPEC §7 "Dividends"): ex-date calendar, future ex-dates, announcements,
increases, best yields, capture stats, REITs/MLPs/closed-end funds and a symbol comparison."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

import numpy as np

from ...analytics import dividends as div_a
from ...models import Dividend
from ...repo import dividends as div_repo
from . import columns as C
from .base import Col, Output, Param, ReportDef, RunParams, group, pick, rel_date, today
from .common import (MIN_MCAP, SCOPE, UNIVERSE_ONLY, bar_lists, base_mask, frame, frame_output, p_int, p_num,
                     p_select)
from .universe import Frame

HISTORY_YEARS = 10
NOTES = (C.NOTE_DIVS, "Yield = the yearly rate of regular dividends ÷ price. One-off special payments are flagged and "
         "left out.")
METHOD = ("Indicated annual rate = latest regular amount × payments per year; special / one-off payments are left out "
          "of the rate, yield, frequency and growth. It needs ≥ 2 stored regular payments (per-symbol dividend "
          "histories are collected weekly) and is blank until then.",)


def _by_symbol(divs: Sequence[Dividend]) -> dict[str, list[Dividend]]:
    out: dict[str, list[Dividend]] = {}
    for d in divs:
        out.setdefault(d.symbol, []).append(d)
    for v in out.values():
        v.sort(key=lambda d: d.ex_date)
    return out


def _history(symbols: Sequence[str] | None, years: int = 3, ahead_days: int = 400) -> dict[str, list[Dividend]]:
    t = today()
    return _by_symbol(div_repo.between(t - timedelta(days=365 * years), t + timedelta(days=ahead_days), symbols=symbols))


def _fval(f: Frame, i: int | None, k: str):
    if i is None:
        return None
    v = f[k][i]
    if isinstance(v, float) and v != v:
        return None
    return v.item() if isinstance(v, np.generic) else v


def _allowed(f: Frame, p: RunParams, sym: str, pos: dict) -> bool:
    i = pos.get(sym)
    if p.get("universe_only") and p.scope is None and (i is None or not f["in_universe"][i]):
        return False
    mc = p.get("min_market_cap")
    return mc is None or (i is not None and np.nan_to_num(f["market_cap"][i]) >= mc)


def _rate_fields(divs: list[Dividend], price: float | None, asof: date) -> dict:
    return {"annual_rate": div_a.annual_rate(divs, asof), "frequency": div_a.frequency(divs, asof),
            "yield": div_a.indicated_yield(divs, price, asof)}


# ============================================================================ event lists
EVENT_COLS = (
    Col("ex_date", "Ex-date", "date"), *C.DESC_SHORT,
    *group("Dividend", (Col("amount", "Amount", "money"), Col("kind", "Type", "badge"),
                        Col("one_off", "One-off", "bool", description="Special / one-off payment: not in the annual "
                            "rate, yield or frequency (its type says so, or it is ≥ 2.5× the usual amount)"),
                        Col("annual_rate", "Annual rate", "money"), Col("yield", "Yield", "pct"),
                        Col("frequency", "Payments / yr", "int", default_hidden=True),
                        Col("declared_date", "Declared", "date"), Col("record_date", "Record", "date", default_hidden=True),
                        Col("pay_date", "Pay date", "date"), Col("days_to_ex", "Days to ex", "int"),
                        Col("source", "Source", "text", description="Where the amount came from: Nasdaq, derived "
                            "from IBKR's dividend-adjusted closes (ibkr-adjusted) or Questrade's current dividend"),
                        Col("discrepancy", "Sources differ", "bool", default_hidden=True,
                            description="The sources' amounts for this ex-date differ by more than $0.01"))),
    Col("price", "Price", "money", C.G_QUOTE), Col("change_pct", "% Chg", "pct_signed", C.G_QUOTE),
    Col("iv30", "IV30", "vol", C.G_VOL),
)


def _event_rows(p: RunParams, by: str, start: date, end: date) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    evs = [d for d in div_repo.between(start, end, by=by, symbols=p.scope) if _allowed(f, p, d.symbol, pos)]
    hist = _history(sorted({d.symbol for d in evs}))
    rows = []
    odd = {s: div_a.one_offs(h) for s, h in hist.items()}
    for d in evs:
        i = pos.get(d.symbol)
        price = _fval(f, i, "price")
        rows.append({"ex_date": d.ex_date, "symbol": d.symbol, "name": _fval(f, i, "name"),
                     "market_cap": _fval(f, i, "market_cap"), "sector": _fval(f, i, "sector"),
                     "amount": d.amount, "kind": d.kind, "declared_date": d.declared_date, "record_date": d.record_date,
                     "pay_date": d.pay_date, "days_to_ex": (d.ex_date - t).days, "price": price,
                     "source": d.source, "discrepancy": d.discrepancy,
                     "change_pct": _fval(f, i, "change_pct"), "iv30": _fval(f, i, "iv30"),
                     "one_off": (d.ex_date, d.amount) in odd.get(d.symbol, div_a.one_offs([d])),
                     **_rate_fields(hist.get(d.symbol, [d]), price, max(t, d.ex_date))})
    return Output(rows, asof=f.asof)


def run_calendar(p: RunParams) -> Output:
    return _event_rows(p, "ex_date", min(p["start"], p["end"]), max(p["start"], p["end"]))


def run_future(p: RunParams) -> Output:
    t = today()
    return _event_rows(p, "ex_date", t, t + timedelta(days=p["days"]))


def run_announcements(p: RunParams) -> Output:
    return _event_rows(p, "declared_date", min(p["start"], p["end"]), max(p["start"], p["end"]))


FILTERS = (MIN_MCAP, UNIVERSE_ONLY, *SCOPE)
CALENDAR = ReportDef(
    "dividend-calendar", "Dividend calendar", "Dividends", "Ex-dividend dates in a date range.",
    (Param("start", "From", "date", today), Param("end", "To", "date", rel_date(14)), *FILTERS),
    EVENT_COLS, run_calendar, ("ex_date", "asc"), NOTES, method=METHOD)
FUTURE = ReportDef(
    "future-ex-dates", "Future ex-dividend dates", "Dividends", "Every announced ex-dividend date from today on.",
    (p_int("days", "Days ahead", 365, 1, 730), *FILTERS), EVENT_COLS, run_future, ("ex_date", "asc"), NOTES, method=METHOD)
ANNOUNCEMENTS = ReportDef(
    "dividend-announcements", "Dividend announcements", "Dividends", "Dividends by declaration date.",
    (Param("start", "Declared from", "date", rel_date(-7)), Param("end", "Declared to", "date", today), *FILTERS),
    EVENT_COLS, run_announcements, ("declared_date", "desc"), NOTES, method=METHOD)

# ============================================================================ increases
INC_COLS = (*C.DESC_SHORT, *group("Dividend", (
    Col("ex_date", "Latest ex-date", "date"), Col("declared_date", "Declared", "date"),
    Col("amount", "New amount", "money"), Col("previous_amount", "Previous amount", "money"),
    Col("increase_pct", "Increase", "pct_signed"), Col("annual_rate", "Annual rate", "money"),
    Col("yield", "Yield", "pct"))), Col("price", "Price", "money", C.G_QUOTE))


def run_increases(p: RunParams) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    since = t - timedelta(days=p["within_days"])
    rows = []
    for sym, divs in _history(p.scope).items():
        if not _allowed(f, p, sym, pos):
            continue
        reg = div_a.regular(divs)
        if len(reg) < 2 or not div_a.is_dividend_increase(divs):
            continue
        last, prev = reg[-1], reg[-2]
        if max(last.ex_date, last.declared_date or last.ex_date) < since:
            continue
        i = pos.get(sym)
        price = _fval(f, i, "price")
        rows.append({"symbol": sym, "name": _fval(f, i, "name"), "market_cap": _fval(f, i, "market_cap"),
                     "sector": _fval(f, i, "sector"), "ex_date": last.ex_date, "declared_date": last.declared_date,
                     "amount": last.amount, "previous_amount": prev.amount, "increase_pct": last.amount / prev.amount - 1,
                     "price": price, **_rate_fields(divs, price, max(t, last.ex_date))})
    return Output(rows, asof=f.asof)


INCREASES = ReportDef(
    "dividend-increases", "Dividend increases", "Dividends",
    "The latest regular dividend is larger than the previous regular one.",
    (p_int("within_days", "Declared/ex within days", 90, 1, 730), *FILTERS), INC_COLS, run_increases,
    ("increase_pct", "desc"), NOTES, method=METHOD)

# ============================================================================ best dividends
BEST_COLS = (*C.DESC_SHORT, Col("price", "Price", "money", C.G_QUOTE), *group("Dividend", (
    Col("yield", "Yield", "pct"), Col("annual_rate", "Annual rate", "money"), Col("frequency", "Payments / yr", "int"),
    Col("payments", "# Payments (10y)", "int"), Col("years_paid", "Years with payments", "int"),
    Col("growth_3y", "Growth 3y (CAGR)", "pct_signed"), Col("growth_5y", "Growth 5y (CAGR)", "pct_signed"),
    Col("next_ex_date", "Next ex-date", "date"))), Col("iv30", "IV30", "vol", C.G_VOL))


def run_best(p: RunParams) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    rows = []
    for sym, divs in _history(p.scope, years=HISTORY_YEARS).items():
        if not _allowed(f, p, sym, pos):
            continue
        reg = [d for d in div_a.regular(divs) if d.ex_date <= t]
        if len(reg) < p["min_payments"]:
            continue
        i = pos.get(sym)
        price = _fval(f, i, "price")
        rf = _rate_fields(divs, price, t)
        if rf["yield"] is None or rf["yield"] < (p["min_yield"] or 0):
            continue
        nxt = next((d.ex_date for d in divs if d.ex_date >= t), None)
        rows.append({"symbol": sym, "name": _fval(f, i, "name"), "market_cap": _fval(f, i, "market_cap"),
                     "sector": _fval(f, i, "sector"), "price": price, **rf, "payments": len(reg),
                     "years_paid": len({d.ex_date.year for d in reg}), "growth_3y": div_a.growth(divs, t, 3),
                     "growth_5y": div_a.growth(divs, t, 5), "next_ex_date": nxt, "iv30": _fval(f, i, "iv30")})
    return Output(rows, asof=f.asof)


BEST = ReportDef(
    "best-dividends", "Best dividends", "Dividends", "Ranked by indicated yield, with payout history.",
    (p_num("min_yield", "Min yield", 0.02, 0, 1, step=0.005), p_int("min_payments", "Min past payments", 8, 0),
     *FILTERS), BEST_COLS, run_best, ("yield", "desc"), NOTES, method=METHOD)

# ============================================================================ capture
CAP_COLS = (*C.DESC_SHORT, Col("price", "Price", "money", C.G_QUOTE),
            *group("Next dividend", (Col("next_ex_date", "Next ex-date", "date"), Col("days_to_ex", "Days", "int"),
                                     Col("next_amount", "Amount", "money"), Col("yield", "Yield", "pct"),
                                     Col("amount_pct", "Amount % of price", "pct"))),
            *group("Historic capture", (Col("captures", "# Ex-dates", "int"),
                                        Col("avg_total_return", "Avg total return", "pct_signed",
                                            description="Buy close before ex, sell close on ex, incl. dividend"),
                                        Col("win_rate", "Win rate", "pct"),
                                        Col("avg_recovery_days", "Avg recovery days", "num"))))


def run_capture(p: RunParams) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    upcoming = {d.symbol: d for d in div_repo.between(t, t + timedelta(days=p["days"]), symbols=p.scope)
                if _allowed(f, p, d.symbol, pos)}
    syms = sorted(upcoming)
    hist = _history(syms, years=p["years"], ahead_days=p["days"] + 1)
    bars = bar_lists(syms, t - timedelta(days=365 * p["years"] + 10))
    rows = []
    for sym in syms:
        nx = upcoming[sym]
        cap = div_a.capture_rows([d for d in hist.get(sym, []) if d.ex_date < t], bars.get(sym, []))
        cs = div_a.capture_stats(cap)
        i = pos.get(sym)
        price = _fval(f, i, "price")
        rows.append({"symbol": sym, "name": _fval(f, i, "name"), "market_cap": _fval(f, i, "market_cap"),
                     "sector": _fval(f, i, "sector"), "price": price, "next_ex_date": nx.ex_date,
                     "days_to_ex": (nx.ex_date - t).days, "next_amount": nx.amount,
                     "yield": _rate_fields(hist.get(sym, [nx]), price, t)["yield"],
                     "amount_pct": nx.amount / price if price else None, "captures": cs["count"],
                     "avg_total_return": cs["avg_total_return"], "win_rate": cs["win_rate"],
                     "avg_recovery_days": cs["avg_recovery_days"]})
    return Output(rows, asof=f.asof)


CAPTURE = ReportDef(
    "dividend-capture", "Dividend capture", "Dividends",
    "Historic dividend-capture statistics for symbols with an ex-date coming up.",
    (p_int("days", "Ex-date within days", 30, 1, 120), p_int("years", "History years", 5, 1, 10), *FILTERS),
    CAP_COLS, run_capture, ("avg_total_return", "desc"),
    (*NOTES, "Capture = buy at the close before the ex-date, sell at the ex-date close; ignores taxes and costs."),
    method=METHOD)

# ============================================================================ REITs / MLPs / CEFs
REIT_COLS = (*C.DESC, *pick(C.QUOTE, "price", "change_pct"), Col("vehicle", "Vehicle", "badge", C.G_DESC),
             C.DIV_YIELD, *pick(C.CATALYSTS, "next_ex_div", "days_to_ex_div"), *pick(C.PERF, "ret_1m", "ret_1y"),
             *pick(C.VOL_CORE, "iv30", "iv_rank"))


def _vehicle(f: Frame) -> np.ndarray:
    out = np.full(len(f), None, dtype=object)
    for i, (ind, name) in enumerate(zip(f["industry"], f["name"])):
        ind_l, name_l = (ind or "").lower(), (name or "").lower()
        if "real estate investment trust" in ind_l or " reit" in f" {name_l}":
            out[i] = "REIT"
        elif " l.p." in name_l or name_l.endswith(" lp") or " lp " in name_l or "partners lp" in name_l:
            out[i] = "MLP"
        elif "fund" in name_l and ("closed" in name_l or "income" in name_l or "trust" in name_l):
            out[i] = "CEF"
    return out


def run_reits(p: RunParams) -> Output:
    f = frame()
    veh = _vehicle(f)
    m = base_mask(f, p) & (veh != None)  # noqa: E711 - elementwise
    if p["vehicle"] != "all":
        m &= veh == p["vehicle"]
    return frame_output(f, m, REIT_COLS, extra={"vehicle": veh})


REITS = ReportDef(
    "reits-mlps", "REITs, MLPs & closed-end funds", "Dividends",
    "Real-estate investment trusts, master limited partnerships and closed-end funds with yield and ex-dates.",
    (p_select("vehicle", "Vehicle", "all", (("all", "All"), ("REIT", "REITs"), ("MLP", "MLPs"), ("CEF", "Closed-end funds"))),
     MIN_MCAP, UNIVERSE_ONLY, *SCOPE),
    REIT_COLS, run_reits, ("div_yield", "desc"),
    (*NOTES, "Classification from the Nasdaq industry (REITs) and the security name (L.P. → MLP, fund names → CEF); "
     "a heuristic."))

# ============================================================================ comparison
CMP_COLS = (Col("symbol", "Symbol", "symbol"), Col("name", "Name", "text"), Col("price", "Price", "money"),
            *group("Dividend", (Col("annual_rate", "Annual rate", "money"), Col("yield", "Yield", "pct"),
                                Col("frequency", "Payments / yr", "int"), Col("growth_1y", "Growth 1y", "pct_signed"),
                                Col("growth_3y", "Growth 3y (CAGR)", "pct_signed"),
                                Col("growth_5y", "Growth 5y (CAGR)", "pct_signed"), Col("payments", "# Payments", "int"),
                                Col("next_ex_date", "Next ex-date", "date"), Col("next_amount", "Next amount", "money"))),
            *group("Capture", (Col("captures", "# Ex-dates", "int"), Col("avg_total_return", "Avg total return", "pct_signed"),
                               Col("win_rate", "Win rate", "pct"), Col("avg_recovery_days", "Avg recovery days", "num"))))


def run_comparison(p: RunParams) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    syms = p.scope or []
    hist = _history(syms, years=HISTORY_YEARS)
    bars = bar_lists(syms, t - timedelta(days=365 * HISTORY_YEARS))
    rows = []
    for sym in syms:
        i = pos.get(sym)
        price = _fval(f, i, "price")
        s = div_a.dividend_summary(sym, hist.get(sym, []), bars.get(sym, []), price, t)
        nx = s["next"] or {}
        rows.append({"symbol": sym, "name": _fval(f, i, "name"), "price": price, "annual_rate": s["annual_rate"],
                     "yield": s["yield"], "frequency": s["frequency"], "growth_1y": s["growth_1y"],
                     "growth_3y": s["growth_3y"], "growth_5y": s["growth_5y"], "payments": len(s["history"]),
                     "next_ex_date": nx.get("ex_date"), "next_amount": nx.get("amount"),
                     "captures": s["capture_stats"]["count"], "avg_total_return": s["capture_stats"]["avg_total_return"],
                     "win_rate": s["capture_stats"]["win_rate"], "avg_recovery_days": s["capture_stats"]["avg_recovery_days"]})
    return Output(rows, asof=f.asof)


COMPARISON = ReportDef(
    "dividend-comparison", "Dividend comparison", "Dividends", "Side-by-side dividend profile of a list of symbols.",
    (Param("symbols", "Symbols", "symbols", ["KO", "PEP", "PG", "JNJ", "XOM", "CVX", "T", "VZ"]),),
    CMP_COLS, run_comparison, ("yield", "desc"), NOTES, method=METHOD)

REPORTS = (CALENDAR, FUTURE, ANNOUNCEMENTS, INCREASES, BEST, CAPTURE, REITS, COMPARISON)
