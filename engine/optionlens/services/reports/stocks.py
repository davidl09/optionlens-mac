"""Stock reports (SPEC §7 "Stocks"): stock/ETF screeners, movers, highs/lows, volume and gap screens,
relative performance, sector/industry performance, seasonality, extended-hours movers and
upcoming events. All but the extended-hours and seasonality reports derive from the universe frame."""
from __future__ import annotations

from dataclasses import replace

import asyncio
import logging
import threading
from datetime import date

import numpy as np

from ...analytics import intraday as intraday_a
from ...analytics.calendar import NY
from ...jobs import extended as extended_job
from ...jobs import ondemand
from ...repo import bars as bars_repo
from ...repo import extended as extended_repo
from ...repo import user as user_repo
from . import columns as C
from .base import Col, Output, Param, ReportDef, RunParams, group, pick, today
from .common import (KIND, MIN_MCAP, MIN_PRICE, SCOPE, UNIVERSE_ONLY, base_mask, frame, frame_output, ge, le,
                     p_int, p_num, p_select)
from .universe import Frame

log = logging.getLogger("optionlens.reports")

MIN_AVG_VOL = p_int("min_avg_volume", "Min avg volume (30d)", 100_000, 0, step=50_000)
NOTES_FRAME = (C.NOTE_IV_RANK, C.NOTE_DIVS, C.NOTE_DELAY)


def _liquid(f: Frame, p: RunParams) -> np.ndarray:
    return base_mask(f, p) & ge(f["avg_volume_30d"], p.get("min_avg_volume"))


# ============================================================================ screeners
def _screener(kind: str):
    def run(p: RunParams) -> Output:
        f = frame()
        m = _liquid(f, p) & (f["kind"] == kind)
        return frame_output(f, m, C.STOCK_SCREENER)
    return run


SCREENER_PARAMS = (MIN_PRICE, Param("min_avg_volume", "Min avg volume (30d)", "int", 0, min=0, step=50_000),
                   MIN_MCAP, UNIVERSE_ONLY, *SCOPE)
STOCK_SCREENER = ReportDef(
    "stock-screener", "Stock screener", "Stocks",
    "30+ metrics per stock: performance, volume, technicals, volatility, option activity, dividends and catalysts.",
    SCREENER_PARAMS, C.STOCK_SCREENER, _screener("stock"), ("market_cap", "desc"), NOTES_FRAME)
ETF_SCREENER = ReportDef(
    "etf-screener", "ETF screener", "Stocks", "The stock-screener metrics for ETFs.",
    (Param("min_price", "Min price", "number", 0.0, min=0), *SCREENER_PARAMS[1:]), C.STOCK_SCREENER,
    _screener("etf"), ("avg_volume_30d", "desc"), NOTES_FRAME)

# ============================================================================ movers
MOVERS_PARAMS = (MIN_PRICE, MIN_AVG_VOL, KIND, UNIVERSE_ONLY, *SCOPE)


def run_market_movers(p: RunParams) -> Output:
    f = frame()
    m = _liquid(f, p) & np.isfinite(f["change_pct"])
    side = p["side"]
    if side == "gainers":
        m &= f["change_pct"] > 0
        sort = ("change_pct", True)
    elif side == "losers":
        m &= f["change_pct"] < 0
        sort = ("change_pct", False)
    else:
        sort = ("volume", True)
    out = frame_output(f, m, C.MOVERS, sort=sort)
    out.presorted = True
    return out


MARKET_MOVERS = ReportDef(
    "market-movers", "Market movers", "Stocks", "Top gainers, losers or most active symbols today.",
    (p_select("side", "Show", "gainers", (("gainers", "Gainers"), ("losers", "Losers"), ("active", "Most active"))),
     *MOVERS_PARAMS), C.MOVERS, run_market_movers, None, NOTES_FRAME)


def _hv_movers(up: bool):
    def run(p: RunParams) -> Output:
        f = frame()
        chg = f["change_pct"]
        m = _liquid(f, p) & ge(f["rel_volume"], p["min_rel_volume"])
        m &= ge(chg, p["min_change"]) if up else le(chg, -p["min_change"])
        return frame_output(f, m, C.MOVERS)
    return run


HV_PARAMS = (p_num("min_rel_volume", "Min relative volume", 1.5, 0, step=0.25),
             p_num("min_change", "Min |% change|", 0.02, 0, step=0.01), *MOVERS_PARAMS)
GAINERS_HV = ReportDef("gainers-high-volume", "Gainers on high volume", "Stocks",
                       "Up today with volume above its 30-day average.", HV_PARAMS, C.MOVERS, _hv_movers(True),
                       ("change_pct", "desc"), NOTES_FRAME)
LOSERS_HV = ReportDef("losers-high-volume", "Losers on high volume", "Stocks",
                      "Down today with volume above its 30-day average.", HV_PARAMS, C.MOVERS, _hv_movers(False),
                      ("change_pct", "asc"), NOTES_FRAME)

HL_COLS = (*C.MOVERS, *group(C.G_TECH, (Col("high_52w", "52w High", "money"), Col("low_52w", "52w Low", "money"),
                                        *pick(C.TECH, "pct_from_high_52w", "pct_from_low_52w"))))


def run_highs_lows(p: RunParams) -> Output:
    f = frame()
    m = _liquid(f, p)
    within = p["within_pct"]
    px = f["price"]
    if p["side"] == "high":
        today_hi = np.where(np.isfinite(f["high"]), f["high"], px)
        m &= (today_hi >= f["prior_high_52w"]) if within == 0 else (px >= f["prior_high_52w"] * (1 - within))
        sort = ("pct_from_high_52w", True)
    else:
        today_lo = np.where(np.isfinite(f["low"]), f["low"], px)
        m &= (today_lo <= f["prior_low_52w"]) if within == 0 else (px <= f["prior_low_52w"] * (1 + within))
        sort = ("pct_from_low_52w", False)
    out = frame_output(f, m & np.isfinite(px), HL_COLS, sort=sort)
    out.presorted = True
    return out


HIGHS_LOWS = ReportDef(
    "52w-highs-lows", "52-week highs / lows", "Stocks",
    "Symbols trading at (or within x% of) a new 52-week high or low today.",
    (p_select("side", "Show", "high", (("high", "New highs"), ("low", "New lows"))),
     p_num("within_pct", "Within % of the prior 52w extreme", 0.0, 0, 0.5, step=0.01,
           help="0 = today's high/low exceeded the prior 52-week extreme"), *MOVERS_PARAMS),
    HL_COLS, run_highs_lows, None, ("52-week range from the prior 252 stored daily bars.",))


def run_unusual_stock_volume(p: RunParams) -> Output:
    f = frame()
    return frame_output(f, _liquid(f, p) & ge(f["rel_volume"], p["min_rel_volume"]), C.MOVERS)


UNUSUAL_STOCK_VOLUME = ReportDef(
    "unusual-stock-volume", "Unusual stock volume", "Stocks", "Today's share volume vs the 30-day average.",
    (p_num("min_rel_volume", "Min relative volume", 2.0, 0, step=0.5), *MOVERS_PARAMS), C.MOVERS,
    run_unusual_stock_volume, ("rel_volume", "desc"),
    ("Intraday, today's volume is partial (delayed quote volume so far) while the average is of full days.",))


def run_volume_burst(p: RunParams) -> Output:
    f = frame()
    m = _liquid(f, p) & ge(f["rel_volume"], p["min_rel_volume"]) & ge(np.abs(f["change_pct"]), p["min_change"])
    return frame_output(f, m, C.MOVERS)


VOLUME_BURST = ReportDef(
    "volume-burst", "Volume burst", "Stocks", "Volume ≥ 3× average with a move of at least 2% either way.",
    (p_num("min_rel_volume", "Min relative volume", 3.0, 0, step=0.5),
     p_num("min_change", "Min |% change|", 0.02, 0, step=0.01), *MOVERS_PARAMS), C.MOVERS, run_volume_burst,
    ("rel_volume", "desc"), NOTES_FRAME)

GAP_COLS = (*C.MOVERS, Col("open", "Open", "money", C.G_QUOTE),
            Col("move_from_open", "Move from open", "pct_signed", C.G_PERF, "price / open − 1"))


def run_gap_and_go(p: RunParams) -> Output:
    f = frame()
    g = f["gap_pct"]
    with np.errstate(divide="ignore", invalid="ignore"):
        mfo = f["price"] / f["open"] - 1.0
    m = _liquid(f, p)
    if p["side"] == "up":
        m &= ge(g, p["min_gap"]) & (np.nan_to_num(mfo) > 0)
        sort = ("gap_pct", True)
    else:
        m &= le(g, -p["min_gap"]) & (np.nan_to_num(mfo) < 0)
        sort = ("gap_pct", False)
    out = frame_output(f, m, GAP_COLS, extra={"move_from_open": mfo}, sort=sort)
    out.presorted = True
    return out


GAP_AND_GO = ReportDef(
    "gap-and-go", "Gap and go", "Stocks", "Gapped at the open by at least x% and kept moving in the gap direction.",
    (p_select("side", "Gap", "up", (("up", "Gap up"), ("down", "Gap down"))),
     p_num("min_gap", "Min |gap|", 0.02, 0, step=0.01), *MOVERS_PARAMS), GAP_COLS, run_gap_and_go, None,
    ("Gap = today's open / previous close − 1; continuation = price beyond the open in the gap direction.",))

# ============================================================================ relative performance
PERIODS = (("1m", "1 month"), ("3m", "3 months"), ("6m", "6 months"), ("1y", "1 year"))
OUT_COLS = (*C.DESC_SHORT, *pick(C.QUOTE, "price", "change_pct"),
            *group(C.G_PERF, (Col("ret_1m", "% Chg 1m", "pct_signed"), Col("ret_3m", "% Chg 3m", "pct_signed"),
                              Col("ret_6m", "% Chg 6m", "pct_signed"), Col("ret_1y", "% Chg 1y", "pct_signed"))),
            *group("Vs SPY", tuple(Col(f"rel_{k}", f"vs SPY {k}", "pct_signed", description=f"Return − SPY return ({v})")
                                   for k, v in PERIODS)),
            *pick(C.TECH, "beta"), *pick(C.VOL_CORE, "iv30"))


def _rel(f: Frame) -> dict[str, np.ndarray]:
    i = f.index().get("SPY")
    out = {}
    for k, _ in PERIODS:
        bench = f[f"ret_{k}"][i] if i is not None else np.nan
        out[f"rel_{k}"] = f[f"ret_{k}"] - bench
    return out


def run_outperformers(p: RunParams) -> Output:
    f = frame()
    rel = _rel(f)
    key = f"rel_{p['period']}"
    m = _liquid(f, p) & ge(rel[key], p["min_outperformance"]) & (f["symbol"] != "SPY")
    out = frame_output(f, m, OUT_COLS, extra=rel, sort=(key, True))
    out.presorted = True
    return out


OUTPERFORMERS = ReportDef(
    "outperformers", "Outperformers vs SPY", "Stocks", "Return minus SPY's return over 1m/3m/6m/1y.",
    (p_select("period", "Period", "3m", PERIODS), p_num("min_outperformance", "Min outperformance", 0.0, step=0.01),
     *MOVERS_PARAMS), OUT_COLS, run_outperformers, None, ("Price returns (no dividends).",))

PERF_KEYS = ("ret_1d", "ret_5d", "ret_1m", "ret_3m", "ret_6m", "ret_ytd", "ret_1y")
GROUP_PERF_COLS = (Col("group", "Group", "text"), Col("symbols", "# Symbols", "int"),
                   Col("market_cap", "Total market cap", "bigmoney"),
                   Col("advancing_pct", "% Advancing", "pct", description="Share of members up today"),
                   *(Col(k, {"ret_1d": "Avg % 1d", "ret_5d": "Avg % 5d", "ret_1m": "Avg % 1m", "ret_3m": "Avg % 3m",
                             "ret_6m": "Avg % 6m", "ret_ytd": "Avg % YTD", "ret_1y": "Avg % 1y"}[k], "pct_signed",
                         C.G_PERF) for k in PERF_KEYS),
                   Col("median_1d", "Median % 1d", "pct_signed", C.G_PERF, default_hidden=True),
                   Col("avg_iv30", "Avg IV30", "vol", C.G_VOL))


def _group_perf(field: str):
    def run(p: RunParams) -> Output:
        f = frame()
        m = base_mask(f, p) & np.isfinite(f["ret_1d"])
        g = f[field]
        rows = []
        for name in sorted({x for x in g[m] if x}):
            s = m & (g == name)
            r = {"group": name, "symbols": int(s.sum()), "market_cap": float(np.nansum(f["market_cap"][s])),
                 "advancing_pct": float(np.mean(f["ret_1d"][s] > 0)),
                 "median_1d": float(np.nanmedian(f["ret_1d"][s]))}
            for k in PERF_KEYS:
                v = f[k][s]
                r[k] = float(np.nanmean(v)) if np.isfinite(v).any() else None
            iv = f["iv30"][s]
            r["avg_iv30"] = float(np.nanmean(iv)) if np.isfinite(iv).any() else None
            rows.append(r)
        return Output(rows, asof=f.asof)
    return run


GROUP_PARAMS = (Param("kind", "Type", "select", "stock", options=KIND.options), UNIVERSE_ONLY, *SCOPE)
PERF_SECTOR = ReportDef("performance-by-sector", "Sector performance", "Stocks",
                        "Equal-weight average returns per sector.", GROUP_PARAMS, GROUP_PERF_COLS,
                        _group_perf("sector"), ("ret_1d", "desc"), ("Equal-weight averages of member returns.",))
PERF_INDUSTRY = ReportDef("performance-by-industry", "Industry performance", "Stocks",
                          "Equal-weight average returns per industry.", GROUP_PARAMS, GROUP_PERF_COLS,
                          _group_perf("industry"), ("ret_1d", "desc"), ("Equal-weight averages of member returns.",))

# ============================================================================ seasonality
SEAS_COLS = (*C.DESC_SHORT, *pick(C.QUOTE, "price", "change_pct"),
             *group("This month", (Col("cur_month", "Month", "int"), Col("cur_avg", "Avg return", "pct_signed"),
                                   Col("cur_median", "Median return", "pct_signed", default_hidden=True),
                                   Col("cur_pos_pct", "% Positive", "pct"), Col("cur_n", "Years", "int"))),
             *group("Next month", (Col("next_month", "Month", "int"), Col("next_avg", "Avg return", "pct_signed"),
                                   Col("next_median", "Median return", "pct_signed", default_hidden=True),
                                   Col("next_pos_pct", "% Positive", "pct"), Col("next_n", "Years", "int"))),
             *pick(C.VOL_CORE, "iv30"))
_seas_lock = threading.Lock()
_seas_cache: dict[date, dict] = {}


def monthly_stats(day: date, years: int = 10) -> dict[str, dict[int, tuple[float, float, float, int]]]:
    """{symbol: {month: (avg, median, pos_pct, n)}} of monthly returns (last close of month / last close of
    the prior month − 1) over the last `years` years, excluding the current month. Cached per day."""
    with _seas_lock:
        if day in _seas_cache:
            return _seas_cache[day]
    c = bars_repo.month_end_closes(date(day.year - years - 1, 12, 1))
    sym, yr, mo, cl = c["symbol"], c["year"].astype(int), c["month"].astype(int), c["close"].astype(float)
    mi = yr * 12 + (mo - 1)
    same = np.zeros(len(sym), bool)
    same[1:] = (sym[1:] == sym[:-1]) & (mi[1:] - mi[:-1] == 1)
    ret = np.full(len(sym), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret[1:] = cl[1:] / cl[:-1] - 1.0
    cur = day.year * 12 + day.month - 1
    keep = same & (mi < cur) & (yr >= day.year - years) & np.isfinite(ret)
    out: dict[str, dict] = {}
    s_k, m_k, r_k = sym[keep], mo[keep], ret[keep]
    order = np.lexsort((m_k, s_k))
    s_k, m_k, r_k = s_k[order], m_k[order], r_k[order]
    if len(s_k):
        bounds = np.flatnonzero((s_k[1:] != s_k[:-1]) | (m_k[1:] != m_k[:-1])) + 1
        for a, b in zip(np.concatenate(([0], bounds)), np.concatenate((bounds, [len(s_k)]))):
            v = r_k[a:b]
            out.setdefault(s_k[a], {})[int(m_k[a])] = (float(v.mean()), float(np.median(v)), float((v > 0).mean()), len(v))
    with _seas_lock:
        _seas_cache.clear()
        _seas_cache[day] = out
    return out


def run_seasonality(p: RunParams) -> Output:
    f = frame()
    d = today()
    stats_ = monthly_stats(d)
    cm, nm = d.month, d.month % 12 + 1
    n = len(f)
    extra = {k: np.full(n, np.nan) for k in ("cur_avg", "cur_median", "cur_pos_pct", "cur_n", "next_avg",
                                             "next_median", "next_pos_pct", "next_n")}
    extra["cur_month"], extra["next_month"] = np.full(n, cm), np.full(n, nm)
    for i, s in enumerate(f["symbol"]):
        st_ = stats_.get(s)
        if not st_:
            continue
        for pre, mm in (("cur", cm), ("next", nm)):
            if mm in st_:
                a, md, pp, k = st_[mm]
                extra[f"{pre}_avg"][i], extra[f"{pre}_median"][i] = a, md
                extra[f"{pre}_pos_pct"][i], extra[f"{pre}_n"][i] = pp, k
    m = _liquid(f, p) & ge(extra["cur_n"], p["min_years"]) & ge(extra["cur_pos_pct"], p["min_pos_pct"])
    return frame_output(f, m, SEAS_COLS, extra=extra)


SEASONALITY = ReportDef(
    "seasonality-screener", "Seasonality screener", "Stocks",
    "Historic average return and % of positive years for the current and next calendar month (up to 10 years).",
    (p_int("min_years", "Min years of history", 5, 1, 10), p_num("min_pos_pct", "Min % positive (this month)", None, 0, 1),
     *MOVERS_PARAMS), SEAS_COLS, run_seasonality, ("cur_avg", "desc"),
    ("Monthly return = last close of the month / last close of the prior month − 1; the current month is excluded.",))

# ============================================================================ extended hours
EXT_COLS = (*C.DESC_SHORT, Col("price", "Last close", "money", C.G_QUOTE), Col("change_pct", "Regular % Chg", "pct_signed", C.G_QUOTE),
            *group("Extended hours", (Col("ext_price", "Ext. price", "money"), Col("ext_change", "Ext. change", "money"),
                                      Col("ext_change_pct", "Ext. % Chg", "pct_signed"),
                                      Col("ext_volume", "Ext. volume", "int", description="Nasdaq rows: extended-hours volume"),
                                      Col("day_volume", "Volume (day)", "int", description="Questrade rows: the day's "
                                          "volume as the quote reports it"),
                                      Col("ext_high", "Ext. high", "money", default_hidden=True),
                                      Col("ext_low", "Ext. low", "money", default_hidden=True),
                                      Col("ext_updated", "Updated", "text"), Col("source", "Source", "text"))),
            C.HALTED, Col("in_watchlist", "In Watchlist", "bool", C.G_DESC), *pick(C.VOL_CORE, "iv30"), *C.CATALYSTS[:2])
EXT_CONCURRENCY = 4
EXT_BASE_KEYS = ("name", "market_cap", "sector", "price", "change_pct", "iv30", "next_earnings", "days_to_earnings",
                 "halted")


def questrade_extended(market: str) -> dict[str, dict]:
    """{symbol: report fields} of the newest session's stored extended-hours quotes (jobs.extended) that show a trade in
    the window: the last trade's time inside it, or (no time sent) a last price that differs from the last
    regular-hours trade."""
    out = {}
    for r in extended_repo.latest_session(market):
        px, reg = r["price"], r["regular_price"]
        a, b = intraday_a.extended_window(r["session_date"], market)
        t = r["last_trade_at"]
        if px is None or reg is None or not (a <= t < b if t is not None else px != reg):
            continue
        out[r["symbol"]] = {"ext_price": px, "ext_change": px - reg, "ext_change_pct": px / reg - 1.0,
                            "day_volume": r["volume"], "ext_updated": f"{(t or r['ts']).astimezone(NY):%H:%M} ET",
                            "source": r["source"], "delayed": r["delayed"]}
    return out


async def _nasdaq_extended(market: str, syms: list[str]) -> dict[str, dict]:
    """{symbol: report fields} from Nasdaq's extended-hours quotes (5-minute cache), for symbols with a trade."""
    sem = asyncio.Semaphore(EXT_CONCURRENCY)

    async def one(s: str):
        async with sem:
            try:
                return s, await ondemand.fetch_cached("extended", s, market=market)
            except Exception as e:  # noqa: BLE001 - one symbol failing must not fail the report
                log.info("extended %s %s: %s", market, s, e)
                return s, None

    return {s: {"ext_price": d.get("price"), "ext_change": d.get("change"), "ext_change_pct": d.get("change_pct"),
                "ext_volume": d.get("volume"), "ext_high": d.get("high"), "ext_low": d.get("low"),
                "ext_updated": d.get("updated"), "source": "nasdaq"}
            for s, d in await asyncio.gather(*(one(s) for s in syms)) if d and d.get("price") is not None}


def _extended(market: str):
    async def run(p: RunParams) -> Output:
        f = await asyncio.to_thread(frame)
        wl = set(await asyncio.to_thread(user_repo.watchlist_symbols))
        qt = await asyncio.to_thread(questrade_extended, market)
        if p.scope is not None:
            wanted = list(p.scope)
        else:   # every symbol Questrade has; Nasdaq for the watchlists and the most active ones it lacks
            m = f["in_universe"] & (f["kind"] != "index") & np.isfinite(f["volume"])
            idx = np.nonzero(m)[0]
            idx = idx[np.argsort(-f["volume"][idx], kind="stable")][: p["top_n"]]
            wanted = list(dict.fromkeys([*sorted(qt), *sorted(wl), *f["symbol"][idx]]))
        fallback = [s for s in wanted if s not in qt]
        nq = await _nasdaq_extended(market, fallback) if fallback else {}
        pos = f.index()
        rows = []
        for s in wanted:
            d = qt.get(s) or nq.get(s)
            if d is None:
                continue
            i = pos.get(s)
            rows.append({**dict.fromkeys(C.keys(EXT_COLS)), "symbol": s,
                         **(f.rows([i], EXT_BASE_KEYS)[0] if i is not None else {}), **d, "in_watchlist": s in wl})
        n_qt = sum(1 for s in wanted if s in qt)
        notes = [f"{n_qt} symbols from Questrade's extended-hours quotes (the whole universe, collected every "
                 f"{extended_job.COLLECT_EVERY_MIN} minutes during {market}-market hours); "
                 f"{len(rows) - n_qt} from Nasdaq (watchlists ∪ top {p['top_n']} by volume without a Questrade quote, "
                 "5-minute cache)."]
        rows.sort(key=lambda r: -abs(r["ext_change_pct"] or 0))
        return Output(rows, notes, asof=f.asof, presorted=True)
    return run


EXT_PARAMS = (p_int("top_n", "Nasdaq fallback: top N by volume", 50, 0, 200,
                    help="Only for symbols without a Questrade extended-hours quote; plus every watchlist symbol"), *SCOPE)
PREMARKET = ReportDef("premarket-movers", "Pre-market movers", "Stocks",
                      "Pre-market moves of every symbol with a pre-market trade.", EXT_PARAMS, EXT_COLS,
                      _extended("pre"), None)
AFTERHOURS = ReportDef("afterhours-movers", "After-hours movers", "Stocks",
                       "After-hours moves of every symbol with an after-hours trade.", EXT_PARAMS, EXT_COLS,
                       _extended("post"), None)

# ============================================================================ event-driven
EVENT_COLS = (*C.DESC_SHORT, Col("event", "Event", "badge"), Col("event_date", "Date", "date"),
              Col("days_to_event", "Days", "int"), Col("detail", "Detail", "text"),
              *pick(C.QUOTE, "price", "change_pct"), *pick(C.VOL_CORE, "iv30", "iv_rank"),
              Col("expected_move30_pct", "30d expected move", "pct", C.G_VOL, "ATM IV30 · √(30/365)"),
              Col("div_yield", "Div Yield", "pct", C.G_DIV))


def run_event_driven(p: RunParams) -> Output:
    f = frame()
    m = base_mask(f, p)
    days, ev = p["days"], p["event"]
    rows = []
    keys = [c.key for c in EVENT_COLS if c.key not in ("event", "event_date", "days_to_event", "detail")]
    if ev in ("all", "earnings"):
        sel = m & le(f["days_to_earnings"], days) & ge(f["days_to_earnings"], 0)
        idx = np.nonzero(sel)[0]
        for i, r in zip(idx, f.rows(idx, keys)):
            r.update(event="Earnings", event_date=f["next_earnings"][i], days_to_event=f["days_to_earnings"][i],
                     detail=(f["earnings_timing"][i] or "unknown").upper())
            rows.append(r)
    if ev in ("all", "ex_div"):
        sel = m & le(f["days_to_ex_div"], days) & ge(f["days_to_ex_div"], 0)
        idx = np.nonzero(sel)[0]
        for i, r in zip(idx, f.rows(idx, keys)):
            amt = f["next_div_amount"][i]
            r.update(event="Ex-dividend", event_date=f["next_ex_div"][i], days_to_event=f["days_to_ex_div"][i],
                     detail=f"${amt:.4g}" if np.isfinite(amt) else None)
            rows.append(r)
    return Output(rows, asof=f.asof)


EVENT_DRIVEN = ReportDef(
    "event-driven", "Event-driven", "Stocks", "Upcoming earnings and ex-dividend dates within N days.",
    (p_int("days", "Within days", 7, 0, 90), p_select("event", "Events", "all",
                                                       (("all", "All"), ("earnings", "Earnings"), ("ex_div", "Ex-dividend"))),
     MIN_MCAP, KIND, UNIVERSE_ONLY, *SCOPE),
    EVENT_COLS, run_event_driven, ("days_to_event", "asc"), (C.NOTE_DIVS,))

# Sector / industry totals never mix markets; pre-market / after-hours quotes (Nasdaq) and the event calendar
# (Nasdaq earnings and dividends) cover US listings only.
REPORTS = (STOCK_SCREENER, ETF_SCREENER, MARKET_MOVERS, GAINERS_HV, LOSERS_HV, HIGHS_LOWS, UNUSUAL_STOCK_VOLUME,
           VOLUME_BURST, GAP_AND_GO, OUTPERFORMERS, replace(PERF_SECTOR, markets="one"),
           replace(PERF_INDUSTRY, markets="one"), SEASONALITY, *(replace(d, markets="us") for d in
                                                                  (PREMARKET, AFTERHOURS, EVENT_DRIVEN)))
