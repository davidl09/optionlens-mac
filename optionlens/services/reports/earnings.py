"""Earnings reports (SPEC §7 "Earnings"): calendar with implied vs historical moves, recent results,
historic pattern screener, earnings option-strategy backtest screener, today's opportunities and the
upcoming summary."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Sequence

import numpy as np

from ...analytics import chainmetrics as cm
from ...analytics import earnings as earn_a
from ...analytics import optbacktest
from ...analytics.calendar import next_trading_day
from ...models import EarningsEvent
from ...repo import bars as bars_repo
from ...repo import earnings as earn_repo
from ...repo import report_snapshots as snaps_repo
from .. import context
from .. import symbol_earnings
from . import columns as C
from .base import Col, Output, Param, ReportDef, RunParams, group, rel_date, today
from .common import MIN_MCAP, SCOPE, UNIVERSE_ONLY, bar_lists, frame, p_int, p_select
from .universe import Frame

CAL_ID = "earnings-calendar"
HISTORY_YEARS = 4
TIME_LABEL = {"bmo": "BMO", "amc": "AMC", "unknown": "—"}
NOTE_TIMING = ("Implied move = ATM straddle of the first expiry on/after the reaction day / spot (reaction day: "
               "event day for BMO, next session for AMC or unknown timing).")
NOTE_HIST = "Historical moves: reaction-day close vs the prior close, from stored daily bars (up to 3y of events)."


# ----------------------------------------------------------------------------- shared helpers
def _events_in(p: RunParams, f: Frame, start: date, end: date) -> list[EarningsEvent]:
    pos = f.index()
    evs = earn_repo.between(start, end, p.scope)
    out = []
    for e in evs:
        i = pos.get(e.symbol)
        if p.get("universe_only") and p.scope is None and (i is None or not f["in_universe"][i]):
            continue
        mc = p.get("min_market_cap")
        if mc is not None and (i is None or not np.nan_to_num(f["market_cap"][i]) >= mc):
            continue
        out.append(e)
    return out


def history_rows(symbols: Sequence[str], before: date | None = None) -> dict[str, list[dict]]:
    """{symbol: EarningsEventRow[] newest first} of past events (date < before, default today)."""
    before = before or today()
    if not symbols:
        return {}
    start = before - timedelta(days=365 * HISTORY_YEARS)
    events: dict[str, list[EarningsEvent]] = {}
    for e in earn_repo.between(start, before - timedelta(days=1), list(symbols)):
        events.setdefault(e.symbol, []).append(e)
    bars = bar_lists(list(events), start - timedelta(days=40))
    return {s: earn_a.event_rows(evs, bars.get(s, [])) for s, evs in events.items() if bars.get(s)}


def implied_moves(events: Iterable[EarningsEvent]) -> dict[str, dict]:
    """{symbol: implied_earnings_move dict} for future events (reaction day after today) from current chains."""
    t = today()
    evs = {e.symbol: e for e in events if symbol_earnings.reaction_day(e) > t}
    if not evs:
        return {}
    far = max((symbol_earnings.reaction_day(e) - t).days for e in evs.values()) + 60
    out = {}
    for ca in context.load_universe_chain_arrays(list(evs), None, 0, far):
        ev = evs[ca.symbol]
        out[ca.symbol] = earn_a.implied_earnings_move(cm.expiry_term_rows(ca), symbol_earnings.reaction_day(ev), ca.spot)
    return out


def _fval(f: Frame, i: int | None, k: str):
    if i is None:
        return None
    v = f[k][i]
    if isinstance(v, float) and v != v:
        return None
    return v.item() if isinstance(v, np.generic) else v


# ============================================================================ calendar
CAL_COLS = (
    Col("date", "Date", "date"), Col("time", "Time", "badge", description="BMO = before open, AMC = after close"),
    *C.DESC_SHORT, Col("price", "Price", "money", C.G_QUOTE), Col("change_pct", "% Chg", "pct_signed", C.G_QUOTE),
    *group("Estimates", (Col("eps_estimate", "EPS est", "num2"), Col("num_estimates", "# Ests", "int"),
                         Col("fiscal_quarter", "Fiscal qtr", "text", default_hidden=True))),
    *group("Implied vs historical", (
        Col("implied_move", "Implied move", "pct", description="Straddle of the first expiry ≥ reaction day / spot"),
        Col("excess_implied_move", "Excess implied", "pct", default_hidden=True,
            description="Single-day event move from the term structure (front vs next expiry)"),
        Col("expiry_used", "Expiry used", "date", default_hidden=True),
        Col("avg_abs_move", "Avg |move| (last 8)", "pct"),
        Col("implied_hist_ratio", "Implied / historical", "ratio"),
        Col("last_move", "Last move", "pct_signed"),
        Col("past_events", "# Past events", "int", default_hidden=True))),
    *group(C.G_VOL, (Col("iv30", "IV30", "vol"), Col("iv_rank", "IV Rank", "pct"))),
)


NOTE_NO_EVENTS = ("No earnings events are stored for this date range yet (the calendar is collected daily for "
                  "today−10 … today+90).")


def _empty_note(out: Output) -> Output:
    if not out.rows:
        out.notes.append(NOTE_NO_EVENTS)
    return out


def calendar_rows(p: RunParams, start: date, end: date) -> tuple[list[dict], Frame]:
    f = frame()
    pos = f.index()
    evs = _events_in(p, f, start, end)
    syms = sorted({e.symbol for e in evs})
    hist = history_rows(syms)
    imp = implied_moves(evs)
    rows = []
    for e in evs:
        i = pos.get(e.symbol)
        h = hist.get(e.symbol, [])
        avg = earn_a.earnings_stats(h[:8])["avg_abs_move"] if h else None
        im = imp.get(e.symbol, {})
        rows.append({
            "date": e.date, "time": TIME_LABEL.get(e.timing, "—"), "timing": e.timing, "symbol": e.symbol,
            "name": _fval(f, i, "name"), "market_cap": _fval(f, i, "market_cap"), "sector": _fval(f, i, "sector"),
            "price": _fval(f, i, "price"), "change_pct": _fval(f, i, "change_pct"),
            "eps_estimate": e.eps_estimate, "num_estimates": e.num_estimates, "fiscal_quarter": e.fiscal_quarter,
            "implied_move": im.get("implied_move_pct"), "excess_implied_move": im.get("excess_implied_move_pct"),
            "expiry_used": im.get("expiry_used"), "avg_abs_move": avg,
            "implied_hist_ratio": earn_a.implied_vs_hist_ratio(im.get("implied_move_pct"), avg),
            "last_move": h[0]["move_pct"] if h else None, "past_events": len(h),
            "iv30": _fval(f, i, "iv30"), "iv_rank": _fval(f, i, "iv_rank")})
    return rows, f


def run_calendar(p: RunParams) -> Output:
    if p["end"] < p["start"]:
        p["start"], p["end"] = p["end"], p["start"]
    rows, f = calendar_rows(p, p["start"], p["end"])
    return _empty_note(Output(rows, asof=f.asof))


CAL_PARAMS = (Param("start", "From", "date", today), Param("end", "To", "date", rel_date(7)), MIN_MCAP, UNIVERSE_ONLY,
              *SCOPE)
CALENDAR = ReportDef(
    CAL_ID, "Earnings Calendar", "Earnings",
    "Upcoming earnings with timing, estimates, the options-implied move vs the average historical move.",
    CAL_PARAMS, CAL_COLS, run_calendar, ("market_cap", "desc"),
    (NOTE_TIMING, NOTE_HIST, "Events whose reaction session has already started show no implied move.",
     C.NOTE_IV_RANK))


# ============================================================================ results
RES_COLS = (
    Col("date", "Date", "date"), Col("time", "Time", "badge"), *C.DESC_SHORT,
    *group("Results", (Col("fiscal_quarter", "Fiscal qtr", "text", default_hidden=True),
                       Col("eps_actual", "EPS actual", "num2"), Col("eps_estimate", "EPS est", "num2"),
                       Col("surprise_pct", "Surprise", "pct_signed"))),
    *group("Stock reaction", (Col("reaction_date", "Reaction day", "date", default_hidden=True),
                              Col("gap_pct", "Gap %", "pct_signed"), Col("move_pct", "Move %", "pct_signed"),
                              Col("max_move_pct", "Max move %", "pct", default_hidden=True),
                              Col("implied_move", "Implied move", "pct", description="Stored earnings-calendar snapshot "
                                  "before the event (blank if none)"),
                              Col("move_vs_implied", "|Move| / implied", "ratio"),
                              Col("post_drift_1", "Post drift 1d", "pct_signed", default_hidden=True),
                              Col("post_drift_5", "Post drift 5d", "pct_signed"))),
)


def run_results(p: RunParams) -> Output:
    if p["end"] < p["start"]:
        p["start"], p["end"] = p["end"], p["start"]
    f = frame()
    pos = f.index()
    evs = _events_in(p, f, p["start"], min(p["end"], today()))
    syms = sorted({e.symbol for e in evs})
    hist = history_rows(syms, before=today() + timedelta(days=1))
    snaps: dict[tuple[str, date], list[tuple[date, float]]] = {}
    for sn in snaps_repo.read_snapshots(CAL_ID, p["start"] - timedelta(days=10), p["end"], syms or None):
        pl = sn["payload"]
        if pl.get("implied_move") is not None and pl.get("date"):
            snaps.setdefault((sn["symbol"], date.fromisoformat(str(pl["date"])[:10])), []).append(
                (sn["date"], pl["implied_move"]))
    rows = []
    for e in evs:
        ev_row = next((r for r in hist.get(e.symbol, []) if r["date"] == e.date), None)
        if ev_row is None:
            continue
        i = pos.get(e.symbol)
        before = [v for d, v in sorted(snaps.get((e.symbol, e.date), [])) if d < ev_row["reaction_date"]]
        imp = before[-1] if before else None
        mv = ev_row["move_pct"]
        rows.append({"date": e.date, "time": TIME_LABEL.get(ev_row["timing"], "—"), "symbol": e.symbol,
                     "name": _fval(f, i, "name"), "market_cap": _fval(f, i, "market_cap"), "sector": _fval(f, i, "sector"),
                     "fiscal_quarter": e.fiscal_quarter, "eps_actual": e.eps_actual, "eps_estimate": e.eps_estimate,
                     "surprise_pct": e.surprise_pct, "reaction_date": ev_row["reaction_date"],
                     "gap_pct": ev_row["gap_pct"], "move_pct": mv, "max_move_pct": ev_row["max_move_pct"],
                     "implied_move": imp, "move_vs_implied": abs(mv) / imp if mv is not None and imp else None,
                     "post_drift_1": ev_row["post_drift_1"], "post_drift_5": ev_row["post_drift_5"]})
    return _empty_note(Output(rows, asof=f.asof))


RESULTS = ReportDef(
    "earnings-results", "Earnings Results", "Earnings",
    "Recent reporters: EPS vs estimate and the stock's gap, move and drift after the report.",
    (Param("start", "From", "date", rel_date(-7)), Param("end", "To", "date", today), MIN_MCAP, UNIVERSE_ONLY, *SCOPE),
    RES_COLS, run_results, ("date", "desc"),
    ("Only events whose reaction-day bar exists are listed (AMC reports appear the next session).",
     "Implied move comes from earnings-calendar snapshots stored at each end-of-day run (blank before that)."))


# ============================================================================ pattern screener
PAT_COLS = (
    Col("date", "Next earnings", "date"), Col("time", "Time", "badge"), *C.DESC_SHORT,
    Col("price", "Price", "money", C.G_QUOTE),
    *group("History", (Col("events", "# Events", "int"), Col("up_pct", "% Up", "pct"),
                       Col("avg_move", "Avg move", "pct_signed"), Col("avg_abs_move", "Avg |move|", "pct"),
                       Col("avg_abs_gap", "Avg |gap|", "pct", default_hidden=True),
                       Col("max_up", "Max up", "pct_signed", default_hidden=True),
                       Col("max_down", "Max down", "pct_signed", default_hidden=True))),
    *group("Drift", (Col("avg_pre_drift_5", "Pre-drift 5d avg", "pct_signed"),
                     Col("pre_drift_5_win", "Pre-drift 5d win %", "pct"),
                     Col("avg_post_drift_5", "Post-drift 5d avg", "pct_signed"),
                     Col("post_drift_5_win", "Post-drift 5d win %", "pct"))),
    *group(C.G_VOL, (Col("iv30", "IV30", "vol"), Col("iv_rank", "IV Rank", "pct"))),
)


def _win(rows: list[dict], k: str) -> float | None:
    v = [r[k] for r in rows if r.get(k) is not None]
    return float(np.mean([x > 0 for x in v])) if v else None


def run_pattern(p: RunParams) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    evs = _events_in(p, f, t, t + timedelta(days=p["window"]))
    hist = history_rows(sorted({e.symbol for e in evs}))
    n = int(p["lookback"])
    rows = []
    for e in evs:
        h = hist.get(e.symbol, [])[:n]
        if len(h) < p["min_events"]:
            continue
        s = earn_a.earnings_stats(h)
        i = pos.get(e.symbol)
        rows.append({"date": e.date, "time": TIME_LABEL.get(e.timing, "—"), "symbol": e.symbol,
                     "name": _fval(f, i, "name"), "market_cap": _fval(f, i, "market_cap"),
                     "sector": _fval(f, i, "sector"), "price": _fval(f, i, "price"), "events": s["count"],
                     "up_pct": s["up_pct"], "avg_move": s["avg_move"], "avg_abs_move": s["avg_abs_move"],
                     "avg_abs_gap": s["avg_abs_gap"], "max_up": s["max_up"], "max_down": s["max_down"],
                     "avg_pre_drift_5": s["avg_pre_drift_5"], "pre_drift_5_win": _win(h, "pre_drift_5"),
                     "avg_post_drift_5": s["avg_post_drift_5"], "post_drift_5_win": _win(h, "post_drift_5"),
                     "iv30": _fval(f, i, "iv30"), "iv_rank": _fval(f, i, "iv_rank")})
    return _empty_note(Output(rows, asof=f.asof))


PATTERN = ReportDef(
    "earnings-pattern-screener", "Earnings Pattern Screener", "Earnings",
    "Upcoming reporters with their historic earnings reaction: % up, average move, pre- and post-event drift.",
    (p_select("lookback", "Past events", "8", (("4", "Last 4"), ("8", "Last 8"), ("12", "Last 12"))),
     p_int("window", "Reporting within days", 14, 0, 90), p_int("min_events", "Min past events", 4, 1, 12),
     MIN_MCAP, UNIVERSE_ONLY, *SCOPE),
    PAT_COLS, run_pattern, ("avg_abs_move", "desc"), (NOTE_HIST, "Drift windows are in trading days."))


# ============================================================================ option strategy screener
STRAT_COLS = (
    *C.DESC_SHORT[:2], Col("strategy", "Strategy", "text", default_hidden=True), Col("label", "Strategy", "text"),
    Col("next_earnings", "Next earnings", "date"), Col("time", "Time", "badge"),
    *group("Backtest", (Col("trades", "# Trades", "int"), Col("win_rate", "Win rate", "pct"),
                        Col("avg_return", "Avg return", "pct_signed", description="% of debit, or of max risk for credit"),
                        Col("median_return", "Median", "pct_signed"), Col("best", "Best", "pct_signed"),
                        Col("worst", "Worst", "pct_signed"),
                        Col("avg_pnl", "Avg $ P/L", "money", description="Per contract unit"),
                        Col("pricing", "Pricing mix", "text", description="real / model priced trades"),
                        Col("real_count", "# Real", "int", default_hidden=True),
                        Col("model_count", "# Model", "int", default_hidden=True))),
)
STRATEGY_OPTIONS = (("all", "All strategies"),
                    *((s.id, s.label) for s in optbacktest.EARNINGS_STRATEGIES))


def run_strategy_screener(p: RunParams) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    if p.scope is not None:
        nxt = context.next_earnings_map()
        targets = [(s, nxt.get(s)) for s in p.scope]
    else:
        evs = _events_in(p, f, t, t + timedelta(days=p["window"]))
        evs.sort(key=lambda e: -np.nan_to_num(f["market_cap"][pos[e.symbol]]) if e.symbol in pos else 0)
        seen: dict[str, EarningsEvent] = {}
        for e in evs:
            seen.setdefault(e.symbol, e)
        targets = list(seen.items())[: p["max_symbols"]]
    covered = bars_repo.coverage([s for s, _ in targets])
    rows, notes = [], []
    skipped = sum(1 for s, _ in targets if s not in covered)
    for sym, ev in targets:
        if sym not in covered:
            continue
        try:
            res = symbol_earnings.strategies(sym, p["lookback"], p["entry"], p["exit"])
        except Exception:  # noqa: BLE001 - unknown/new symbols without history are skipped, counted below
            skipped += 1
            continue
        i = pos.get(sym)
        for r in res["rows"]:
            if p["strategy"] != "all" and r["strategy"] != p["strategy"]:
                continue
            if r["trades"] < p["min_trades"]:
                continue
            rows.append({**r, "symbol": sym, "name": _fval(f, i, "name"),
                         "next_earnings": ev.date if ev else None, "time": TIME_LABEL.get(ev.timing, "—") if ev else None,
                         "pricing": f"{r['real_count']} real / {r['model_count']} model"})
    if skipped:
        notes.append(f"{skipped} symbol(s) skipped (no price/earnings history).")
    if p.scope is None and len(targets) == p["max_symbols"]:
        notes.append(f"Limited to the {p['max_symbols']} largest upcoming reporters (raise max_symbols or pass symbols).")
    return _empty_note(Output(rows, notes, asof=f.asof))


STRATEGY_SCREENER = ReportDef(
    "earnings-option-strategy-screener", "Earnings Option Strategy Screener", "Earnings",
    "Backtests 30 option and stock strategies around each upcoming reporter's past earnings events "
    "(entry/exit in trading days relative to the reaction day).",
    (p_int("window", "Reporting within days", 7, 0, 60), p_int("lookback", "Past events", 8, 1, 12),
     p_int("entry", "Entry (trading days)", -1, -10, -1, help="−1 = close before the reaction day"),
     p_int("exit", "Exit (trading days)", 1, 0, 10, help="+1 = close one session after the reaction day"),
     Param("strategy", "Strategy", "select", "all", options=STRATEGY_OPTIONS),
     p_int("min_trades", "Min trades", 3, 0, 12), p_int("max_symbols", "Max symbols", 40, 1, 300),
     MIN_MCAP, UNIVERSE_ONLY, *SCOPE),
    STRAT_COLS, run_strategy_screener, ("avg_return", "desc"),
    ("Real stored EOD option mids are used when available (spread ≤ 25%); otherwise Black-Scholes with the "
     "SPEC IV proxy — see the pricing mix. Option history starts with the collector, so most trades are model-priced.",
     "Expiry = first listed expiry on/after the exit date (≥ 2 days after for short premium).",
     optbacktest.EVENT_VOL_NOTE))


# ============================================================================ today's opportunities
OPP_COLS = (*CAL_COLS[:2], *CAL_COLS[2:], Col("move_gap", "Implied − historical", "pct_signed", "Implied vs historical",
                                             "Implied move minus average |move| (positive = options price a bigger move)"))


def run_opportunities(p: RunParams) -> Output:
    t = today()
    rows, f = calendar_rows(p, t, next_trading_day(t))
    for r in rows:
        r["move_gap"] = (r["implied_move"] - r["avg_abs_move"]
                         if r["implied_move"] is not None and r["avg_abs_move"] is not None else None)
    return _empty_note(Output(rows, asof=f.asof))


OPPORTUNITIES = ReportDef(
    "earnings-todays-opportunities", "Today's Earnings Opportunities", "Earnings",
    "Today's and the next session's reporters ranked by the gap between implied and historical moves.",
    (MIN_MCAP, UNIVERSE_ONLY, *SCOPE), OPP_COLS, run_opportunities, ("move_gap", "desc"), (NOTE_TIMING, NOTE_HIST))

# ============================================================================ upcoming summary
SUM_COLS = (Col("date", "Date", "date"), Col("count", "# Reporting", "int"), Col("bmo", "BMO", "int"),
            Col("amc", "AMC", "int"), Col("unknown", "Time n/a", "int"),
            Col("total_market_cap", "Total market cap", "bigmoney"),
            Col("notable", "Notable (top 5 by market cap)", "text"))


def run_summary(p: RunParams) -> Output:
    f = frame()
    pos = f.index()
    t = today()
    evs = _events_in(p, f, t, t + timedelta(days=p["days"]))
    by: dict[date, list[EarningsEvent]] = {}
    for e in evs:
        by.setdefault(e.date, []).append(e)
    rows = []
    for d in sorted(by):
        es = by[d]
        caps = [(np.nan_to_num(f["market_cap"][pos[e.symbol]]) if e.symbol in pos else 0.0, e.symbol) for e in es]
        caps.sort(reverse=True)
        rows.append({"date": d, "count": len(es), "bmo": sum(e.timing == "bmo" for e in es),
                     "amc": sum(e.timing == "amc" for e in es), "unknown": sum(e.timing == "unknown" for e in es),
                     "total_market_cap": float(sum(c for c, _ in caps)), "notable": ", ".join(s for _, s in caps[:5])})
    return _empty_note(Output(rows, asof=f.asof))


SUMMARY = ReportDef(
    "earnings-upcoming-summary", "Upcoming Earnings Summary", "Earnings",
    "Per day for the next 30 days: number of reporters, BMO/AMC split, total market cap and notable names.",
    (p_int("days", "Days ahead", 30, 1, 90), MIN_MCAP, UNIVERSE_ONLY), SUM_COLS, run_summary, ("date", "asc"))

REPORTS = (CALENDAR, RESULTS, PATTERN, STRATEGY_SCREENER, OPPORTUNITIES, SUMMARY)
