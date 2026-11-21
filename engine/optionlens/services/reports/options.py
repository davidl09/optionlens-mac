"""Options reports (SPEC §7 "Options"): unusual volume, IV rankings/movers, volume leaders, OI
changes, large-volume contracts, the per-contract option screener, the broad view by sector and
protective-put cost."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from ...analytics import probability as prob
from ...analytics import stats
from ...analytics.calendar import next_trading_day, trading_days
from ...repo import metrics as metrics_repo
from ...repo import options as options_repo
from ...repo import report_snapshots as snaps_repo
from .. import context
from . import columns as C
from .base import Col, Output, ReportDef, RunParams, group, pick, today
from .common import (KIND, SCOPE, UNIVERSE_ONLY, bar_lists, base_mask, columnar_rows, frame, frame_output, ge, le, p_int,
                     p_num, p_select)

UOV_ID = "unusual-option-volume"
NOTE_PROXY_VOLUME = ("Option volume is the day's total per contract from the delayed CBOE chain; there is no "
                     "trade-level tape, so sizes are not individual trades and buy/sell side is unknown.")
RIGHT = p_select("right", "Type", "all", (("all", "Calls & puts"), ("C", "Calls"), ("P", "Puts")))


def _right(p: RunParams) -> str | None:
    return None if p.get("right") in (None, "all") else p["right"]


# ============================================================================ unusual option volume
UOV_COLS = (
    *C.DESC,
    *group(C.G_OPT, (
        Col("option_volume", "Option Volume", "int"),
        Col("avg_option_volume_20d", "Avg Volume (20d)", "int",
            description="Mean daily option volume of up to 20 prior collected days"),
        Col("avg_days", "Avg days", "int", description="Prior collected days in the average (target 20)"),
        Col("rel_volume", "Relative Volume", "ratio", description="Option volume / average option volume"),
        Col("vol_oi", "Vol / OI", "ratio", description="Option volume / total open interest"),
        Col("criterion", "Criterion", "badge",
            description="Relative volume; or, for symbols with no prior collected day, the interim Vol / OI test"),
    )),
    *pick(C.FLOW, "call_volume", "put_volume", "pct_call", "pct_put", "call_premium", "put_premium"),
    *C.MONEYNESS,
    Col("net_delta_volume", "Vol-weighted net delta", "int", C.G_OPT,
        "Σ volume × delta × 100 (shares); unsigned by trade side (proxy)"),
    Col("sentiment", "Sentiment", "badge", C.G_OPT, "Bullish if % call ≥ 70%, Bearish if % put ≥ 70%"),
    *group(C.G_QUOTE, (Col("prev_close", "Prev Close", "money"), Col("price", "Price", "money"),
                       Col("change_pct", "% Chg", "pct_signed"))),
    *C.CATALYSTS,
    *pick(C.VOL_CORE, "iv30"), *pick(C.IV_CHANGES, "iv30_chg_pct"), *pick(C.VOL_CORE, "iv_rank"),
    *group("History (lookback)", (
        Col("record_date", "Record Date", "date"),
        Col("appearances", "# Appearances", "int", description="Times listed within the lookback window"),
        Col("close", "Close", "money"),
        Col("next_close", "Next Close", "money"),
        Col("next_day_chg", "% Chg Next Day", "pct_signed"),
    )),
)
_UOV_RENAME = {"rel_volume": "rel_option_volume", "avg_days": "opt_history_days"}
CRIT_REL, CRIT_INTERIM = "Rel. volume", "Vol/OI (interim)"


def run_uov(p: RunParams) -> Output:
    """Relative option volume ≥ min_rel_volume for symbols with ≥ 1 prior collected day; symbols with
    none (fresh install / new symbol) qualify on the labelled interim test volume / total OI ≥ min_vol_oi."""
    if p["lookback_days"] > 0:
        return _uov_history(p)
    f = frame()
    ov, oi = f["option_volume"], f["total_oi"]
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_oi = np.where(oi > 0, ov / oi, np.nan)
    has_avg = np.nan_to_num(f["opt_history_days"]) > 0
    hit = np.where(has_avg, ge(f["rel_option_volume"], p["min_rel_volume"]), ge(vol_oi, p["min_vol_oi"]))
    m = base_mask(f, p) & ge(ov, p["min_volume"]) & hit
    hist_keys = ("record_date", "appearances", "close", "next_close", "next_day_chg")
    none = np.full(len(f), None, dtype=object)
    extra = {k: none for k in hist_keys}
    extra.update(vol_oi=vol_oi, criterion=np.where(has_avg, CRIT_REL, CRIT_INTERIM).astype(object))
    out = frame_output(f, m, UOV_COLS, rename=_UOV_RENAME, extra=extra,
                       sort=("rel_volume", True) if f.opt_history_days > 0 else ("vol_oi", True))
    if not out.rows:
        out.notes.append("No symbol qualifies right now: option volume ≥ "
                         f"{p['min_volume']:,} and relative option volume ≥ {p['min_rel_volume']:g}× its average "
                         f"(or, with no prior collected day, volume / open interest ≥ {p['min_vol_oi']:g})."
                         if len(f) and np.isfinite(ov).any() else "No option chains collected yet (first sweep pending).")
    n_interim = int((m & ~has_avg).sum())
    if n_interim:
        out.notes.append(f"Interim criterion for {n_interim} symbol(s) with no prior collected day of option "
                         f"volume: option volume / total open interest ≥ {p['min_vol_oi']:g} (relative volume "
                         "needs an average, which is never estimated).")
    if 0 < f.opt_history_days < stats.OPTION_VOLUME_AVG_DAYS:
        out.notes.append(f"Average option volume uses the {f.opt_history_days} prior collected day(s) available so "
                         f"far (target {stats.OPTION_VOLUME_AVG_DAYS}); see the Avg days column.")
    return out


def _uov_history(p: RunParams) -> Output:
    dates = [d for d in snaps_repo.snapshot_dates(UOV_ID) if d <= today()][-p["lookback_days"]:]
    if not dates:
        return Output([], ["No stored daily snapshots yet (written at each end-of-day run)."])
    snaps = snaps_repo.read_snapshots(UOV_ID, dates[0], dates[-1], p.scope)
    counts: dict[str, int] = {}
    for s in snaps:
        counts[s["symbol"]] = counts.get(s["symbol"], 0) + 1
    syms = sorted(counts)
    closes = {s: {b.date: b.close for b in bl} for s, bl in bar_lists(syms, dates[0]).items()}   # + today's live bar
    rows = []
    for s in snaps:
        r = dict(s["payload"])
        d, sym = s["date"], s["symbol"]
        cl = closes.get(sym, {})
        close = cl.get(d, r.get("price"))
        nxt = cl.get(next_trading_day(d))
        r.update(record_date=d, appearances=counts[sym], close=close, next_close=nxt,
                 next_day_chg=(nxt / close - 1.0) if nxt and close else None)
        rows.append(r)
    rows.sort(key=lambda r: (r["record_date"], r.get("rel_volume") or 0), reverse=True)
    return Output(rows, [f"Historic snapshots from {dates[0]} to {dates[-1]} ({len(dates)} day(s))."])


UOV = ReportDef(
    UOV_ID, "Unusual Option Volume", "Options",
    "Symbols whose option volume today is a multiple of their 20-day average, with call/put mix, premium, "
    "moneyness breakdown and upcoming catalysts.",
    (p_num("min_rel_volume", "Min relative volume", 2.0, 0, step=0.5),
     p_int("min_volume", "Min option volume", 1000, 0, step=500),
     p_num("min_vol_oi", "Interim: min volume / OI", 0.5, 0, step=0.05,
           help="Used only for symbols with no prior collected day of option volume"),
     p_int("lookback_days", "Lookback days", 0, 0, 10, help="> 0: show stored daily snapshots instead"),
     KIND, *SCOPE),
    UOV_COLS, run_uov, ("rel_volume", "desc"),
    (NOTE_PROXY_VOLUME, C.NOTE_OPT_AVG, C.NOTE_IV_RANK, C.NOTE_DIVS))


# ============================================================================ IV rankings / movers
IVR_COLS = (*C.DESC_SHORT, *pick(C.QUOTE, "price", "change_pct"), *C.VOL_CORE[:3], *C.IV_CHANGES[::2],
            *pick(C.VOL_CORE, "hv20", "iv_hv_ratio"), *C.TERM, Col("option_volume", "Option Volume", "int", C.G_OPT),
            *C.CATALYSTS[:2])
IVR_SORTS = (("iv_rank", "IV rank"), ("iv30", "IV30"), ("iv_pct", "IV percentile"), ("iv_hv_ratio", "IV/HV"),
             ("iv30_chg_1d", "IV30 1d change"), ("term_slope", "Term slope"), ("skew25_30", "Skew"))


def run_iv_rankings(p: RunParams) -> Output:
    f = frame()
    m = base_mask(f, p) & np.isfinite(f["iv30"]) & ge(f["option_volume"], p["min_option_volume"])
    out = frame_output(f, m, IVR_COLS, sort=(p["sort"], True))
    out.presorted = True
    return out


IV_RANKINGS = ReportDef(
    "iv-rankings", "IV Rankings", "Options",
    "IV30 with its 52-week rank and percentile, recent IV changes, HV20, IV/HV, IV60/IV90, term slope and skew.",
    (p_select("sort", "Sort by", "iv_rank", IVR_SORTS), p_int("min_option_volume", "Min option volume", 100, 0),
     KIND, *SCOPE),
    IVR_COLS, run_iv_rankings, None, (C.NOTE_IV_RANK,))

IVM_COLS = (*C.DESC_SHORT, *pick(C.QUOTE, "price", "change_pct"), Col("iv30", "IV30", "vol", C.G_VOL),
            Col("iv30_prev", "Prev IV30", "vol", C.G_VOL), *C.IV_CHANGES, *pick(C.VOL_CORE, "iv_rank", "hv20"),
            Col("option_volume", "Option Volume", "int", C.G_OPT), *C.CATALYSTS[:2])


def run_iv_movers(p: RunParams) -> Output:
    f = frame()
    chg = f["iv30_chg_pct"]
    m = base_mask(f, p) & np.isfinite(chg) & ge(f["option_volume"], p["min_option_volume"])
    side = p["side"]
    if side == "up":
        m &= chg > 0
        sort = ("iv30_chg_pct", True)
    elif side == "down":
        m &= chg < 0
        sort = ("iv30_chg_pct", False)
    else:
        sort = ("_abs", True)
    extra = {"_abs": np.abs(chg)} if side == "both" else None
    out = frame_output(f, m, IVM_COLS, extra=extra, sort=sort)
    out.presorted = True
    if not np.isfinite(f["iv30_chg_1d"]).any():
        out.notes.append("Needs two stored days of IV30; IV changes are blank until the second collection day.")
    return out


IV_MOVERS = ReportDef(
    "iv-movers", "IV Movers", "Options", "Biggest one-day IV30 changes, up or down (vol points and percent).",
    (p_select("side", "Direction", "both", (("both", "Up & down"), ("up", "Up"), ("down", "Down"))),
     p_int("min_option_volume", "Min option volume", 500, 0), KIND, *SCOPE),
    IVM_COLS, run_iv_movers, None, ("IV change compares today's IV30 with the previous stored day.",))

# ============================================================================ volume leaders
OVL_COLS = (*C.DESC_SHORT, Col("kind", "Type", "badge", C.G_DESC), *pick(C.QUOTE, "price", "change_pct"),
            *pick(C.OPT, "option_volume"), *pick(C.FLOW, "call_volume", "put_volume"), *pick(C.OPT, "pc_volume_ratio"),
            Col("total_premium", "Premium", "bigmoney", C.G_OPT, "Σ volume × mid × 100, calls + puts"),
            *pick(C.OPT, "avg_option_volume_20d", "rel_option_volume", "total_oi"), *pick(C.VOL_CORE, "iv30", "iv_rank"))


def run_volume_leaders(p: RunParams) -> Output:
    f = frame()
    m = base_mask(f, p) & ge(f["option_volume"], p["min_volume"])
    return frame_output(f, m, OVL_COLS, sort=("option_volume", True))


VOLUME_LEADERS = ReportDef(
    "option-volume-leaders", "Option Volume Leaders", "Options",
    "Highest total option volume today with call/put split, put/call ratio, premium and volume vs average.",
    (p_int("min_volume", "Min option volume", 1000, 0), KIND, *SCOPE),
    OVL_COLS, run_volume_leaders, ("option_volume", "desc"), (NOTE_PROXY_VOLUME, C.NOTE_OPT_AVG))


# ============================================================================ per-contract shared columns
CONTRACT = (
    Col("symbol", "Symbol", "symbol"), Col("osi", "Contract", "osi", default_hidden=True),
    Col("expiry", "Expiry", "date"), Col("dte", "DTE", "int"), Col("right", "Type", "badge"),
    Col("strike", "Strike", "money"),
)


def _dte(expiry: np.ndarray, day: date) -> np.ndarray:
    return (expiry.astype("datetime64[D]") - np.datetime64(day)).astype(int)


def _mid(bid, ask, last):
    both = (bid > 0) & (ask > 0)
    return np.where(both, (bid + ask) / 2.0, last)


# ============================================================================ OI changes
OI_COLS = (*CONTRACT, Col("oi", "OI", "int"), Col("oi_prev", "OI prior day", "int"),
           Col("oi_change", "OI Change", "int"), Col("oi_change_pct", "% Change", "pct_signed"),
           Col("volume", "Volume", "int"), Col("iv", "IV", "vol"), Col("delta", "Delta", "num2"),
           Col("underlying", "Underlying", "money"), Col("moneyness_pct", "Moneyness %", "pct_signed"))


def run_oi_changes(p: RunParams) -> Output:
    day = metrics_repo.latest_date() or today()
    prior = options_repo.prior_eod_date(day)
    if prior is None:
        return Output([], ["Needs a stored end-of-day open-interest snapshot before the current chain "
                           "(option_eod starts with the first EOD run); empty until then."])
    t = today()
    cols = options_repo.latest_vs_eod_oi(prior, p.scope, p["min_change"], t + timedelta(days=p["dte_min"]),
                                         t + timedelta(days=p["dte_max"]), limit=20_000)
    chg = cols["oi"].astype(float) - cols["oi_prev"].astype(float)
    side = p["side"]
    keep = np.ones(len(chg), bool) if side == "both" else (chg > 0 if side == "increase" else chg < 0)
    cols = {k: v[keep] for k, v in cols.items()}
    chg = chg[keep]
    with np.errstate(divide="ignore", invalid="ignore"):
        cols.update(oi_change=chg, oi_change_pct=np.where(cols["oi_prev"] > 0, chg / cols["oi_prev"], np.nan),
                    dte=_dte(cols["expiry"], t), moneyness_pct=cols["strike"] / cols["underlying"] - 1.0)
    cols["_abs"] = np.abs(chg)
    rows, trunc = columnar_rows(cols, [c.key for c in OI_COLS], ("_abs", True))
    notes = [f"OI change vs end-of-day {prior}."]
    gap = len(trading_days(prior, day)) - 2          # sessions between the prior archive and the current one
    if gap > 0:
        notes.append(f"Gap: the previous end-of-day open interest ({prior}) is {gap + 1} sessions old "
                     f"({gap} session(s) without an archive), so changes span several sessions.")
    return Output(rows, notes, truncated=trunc, presorted=True)


OI_CHANGES = ReportDef(
    "oi-changes", "Open Interest Changes", "Options",
    "Contracts with the largest open-interest change versus the previous stored end-of-day snapshot.",
    (p_int("min_change", "Min |OI change|", 500, 0), p_select("side", "Direction", "both",
                                                              (("both", "Both"), ("increase", "Increases"), ("decrease", "Decreases"))),
     p_int("dte_min", "Min DTE", 0, 0), p_int("dte_max", "Max DTE", 730, 0), *SCOPE),
    OI_COLS, run_oi_changes, None,
    ("Contracts not present in the prior end-of-day snapshot are skipped.", "OCC open interest updates once per day.",
     C.NOTE_CHAIN_UNIVERSE))


# ============================================================================ large-volume contracts
LVC_COLS = (*CONTRACT, Col("volume", "Volume", "int"), Col("oi", "OI", "int"), Col("vol_oi", "Vol/OI", "ratio"),
            Col("mid", "Mid", "money"), Col("premium", "Premium", "bigmoney", description="volume × mid × 100"),
            Col("iv", "IV", "vol"), Col("delta", "Delta", "num2"),
            Col("delta_volume", "Delta volume", "int", description="volume × delta × 100 shares (unsigned by side)"),
            Col("underlying", "Underlying", "money"), Col("moneyness_pct", "Moneyness %", "pct_signed"))


def run_large_volume(p: RunParams) -> Output:
    t = today()
    min_prem = p["min_premium"]
    load_vol = 1 if min_prem is not None else p["min_volume"]
    cols = options_repo.latest_columns(p.scope, _right(p), t + timedelta(days=p["dte_min"]),
                                       t + timedelta(days=p["dte_max"]), None, load_vol)
    mid = _mid(cols["bid"], cols["ask"], cols["last"])
    vol_ = cols["volume"].astype(float)
    prem = vol_ * np.nan_to_num(mid) * 100.0
    keep = vol_ >= p["min_volume"]
    if min_prem is not None:
        keep |= prem >= min_prem
    sel = {k: cols[k][keep] for k in ("symbol", "osi", "expiry", "strike", "right", "volume", "oi", "iv", "delta",
                                      "underlying")}
    with np.errstate(divide="ignore", invalid="ignore"):
        sel.update(mid=mid[keep], premium=prem[keep], dte=_dte(sel["expiry"], t),
                   vol_oi=np.where(sel["oi"] > 0, sel["volume"] / sel["oi"], np.nan),
                   delta_volume=sel["volume"] * sel["delta"] * 100.0,        # unknown delta -> null, never 0
                   moneyness_pct=sel["strike"] / sel["underlying"] - 1.0)
    rows, trunc = columnar_rows(sel, [c.key for c in LVC_COLS], ("volume", True))
    return Output(rows, truncated=trunc)


LARGE_VOLUME = ReportDef(
    "large-volume-contracts", "Large Volume Contracts", "Options",
    "Single contracts with large daily volume or premium (a proxy for large trades).",
    (p_int("min_volume", "Min volume", 2000, 0, step=500),
     p_num("min_premium", "Min premium ($)", 1_000_000, 0, help="Also include contracts with premium ≥ this"),
     RIGHT, p_int("dte_min", "Min DTE", 0, 0), p_int("dte_max", "Max DTE", 365, 0), *SCOPE),
    LVC_COLS, run_large_volume, ("volume", "desc"),
    ("Proxy for large trades: per-contract daily volume, not individual prints (no trade tape is available); "
     "side is unknown.", C.NOTE_CHAIN_UNIVERSE))


# ============================================================================ option screener
OS_COLS = (*CONTRACT, *group("Quote", (Col("bid", "Bid", "money"), Col("ask", "Ask", "money"), Col("mid", "Mid", "money"),
                                       Col("last", "Last", "money", default_hidden=True),
                                       Col("spread_pct", "Spread %", "pct"))),
           *group("Activity", (Col("volume", "Volume", "int"), Col("oi", "OI", "int"), Col("vol_oi", "Vol/OI", "ratio"))),
           *group("Greeks", (Col("iv", "IV", "vol"), Col("delta", "Delta", "num2"), Col("gamma", "Gamma", "num2", default_hidden=True),
                             Col("theta", "Theta", "num2"), Col("vega", "Vega", "num2", default_hidden=True))),
           *group("Value", (Col("moneyness_pct", "Moneyness %", "pct_signed"), Col("breakeven", "Breakeven", "money"),
                            Col("premium_pct", "Premium % of stock", "pct"),
                            Col("annualized_yield", "Annualized yield", "pct", description="premium % · 365 / DTE"),
                            Col("prob_itm", "Prob ITM", "pct", description="Lognormal, contract IV, drift r − q"))),
           Col("underlying", "Underlying", "money"), Col("iv_rank", "IV Rank", "pct", C.G_VOL),
           Col("earnings_before_expiry", "Earnings before expiry", "bool", C.G_CAT))


def run_option_screener(p: RunParams) -> Output:
    chains = context.load_universe_chain_arrays(p.scope, _right(p), p["dte_min"], p["dte_max"], p["min_oi"],
                                                p["min_volume"])
    parts = {k: [] for k in ("symbol", "osi", "expiry", "dte", "right", "strike", "bid", "ask", "mid", "last",
                             "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "underlying", "iv_rank",
                             "earnings_before_expiry", "t", "mu")}
    for ca in chains:
        n = len(ca)
        if not n:
            continue
        exp = np.array(ca.expiries, dtype=object)[ca.exp_idx]
        parts["symbol"].append(np.full(n, ca.symbol, dtype=object))
        parts["osi"].append(ca.osi)
        parts["expiry"].append(exp)
        parts["dte"].append(ca.dte)
        parts["right"].append(np.where(ca.is_call, "C", "P").astype(object))
        for k in ("strike", "bid", "ask", "mid", "last", "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "t"):
            parts[k].append(getattr(ca, k))
        parts["underlying"].append(np.full(n, ca.spot))
        parts["iv_rank"].append(np.full(n, np.nan if ca.iv_rank is None else ca.iv_rank))
        parts["mu"].append(np.full(n, ca.r - ca.q))
        parts["earnings_before_expiry"].append(ca.earnings_before_expiry()[ca.exp_idx])
    if not parts["symbol"]:
        return Output([])
    c = {k: np.concatenate(v) for k, v in parts.items()}
    is_call = c["right"] == "C"
    S, K = c["underlying"], c["strike"]
    with np.errstate(divide="ignore", invalid="ignore"):
        qmid = (c["bid"] + c["ask"]) / 2.0
        c["spread_pct"] = np.where(qmid > 0, (c["ask"] - c["bid"]) / qmid, np.nan)
        c["vol_oi"] = np.where(c["oi"] > 0, c["volume"] / c["oi"], np.nan)
        c["moneyness_pct"] = K / S - 1.0
        c["breakeven"] = np.where(is_call, K + c["mid"], K - c["mid"])
        c["premium_pct"] = c["mid"] / S
        c["annualized_yield"] = np.where(c["dte"] > 0, c["premium_pct"] * 365.0 / c["dte"], np.nan)
        pa = prob.prob_above(S, K, c["t"], np.nan_to_num(c["iv"], nan=0.0), c["mu"])
        c["prob_itm"] = np.where(np.isfinite(c["iv"]), np.where(is_call, pa, 1.0 - pa), np.nan)
    ad = np.abs(c["delta"])
    keep = ge(ad, p["delta_min"]) & le(ad, p["delta_max"])
    keep &= ge(c["moneyness_pct"], p["moneyness_min"]) & le(c["moneyness_pct"], p["moneyness_max"])
    keep &= le(c["spread_pct"], p["max_spread_pct"])
    if p["min_bid"] is not None:
        keep &= ge(c["bid"], p["min_bid"])
    c = {k: v[keep] for k, v in c.items()}
    rows, trunc = columnar_rows(c, [col.key for col in OS_COLS], ("volume", True))
    return Output(rows, truncated=trunc)


OPTION_SCREENER = ReportDef(
    "option-screener", "Option Screener", "Options",
    "Per-contract screener over the universe: price, spread, volume/OI, greeks, moneyness, breakeven, "
    "premium yield, probability ITM and earnings-before-expiry flag.",
    (RIGHT, p_int("dte_min", "Min DTE", 7, 0), p_int("dte_max", "Max DTE", 60, 0),
     p_num("delta_min", "Min |delta|", None, 0, 1, step=0.05), p_num("delta_max", "Max |delta|", None, 0, 1, step=0.05),
     p_num("moneyness_min", "Min moneyness", None, help="strike/spot − 1, e.g. -0.1"),
     p_num("moneyness_max", "Max moneyness", None, help="strike/spot − 1, e.g. 0.1"),
     p_int("min_oi", "Min OI", 100, 0), p_int("min_volume", "Min volume", 0, 0),
     p_num("max_spread_pct", "Max spread % of mid", 0.25, 0, step=0.05), p_num("min_bid", "Min bid", 0.05, 0),
     *SCOPE),
    OS_COLS, run_option_screener, ("volume", "desc"),
    ("SPX/SPXW, NDX/NDXP and RUT/RUTW duplicates are collapsed to the PM-settled weekly root.",
     C.NOTE_CHAIN_UNIVERSE))


# ============================================================================ broad view
BV_COLS = (Col("group", "Sector / group", "text"), Col("symbols", "# Symbols", "int"),
           Col("call_volume", "Call Vol", "int"), Col("put_volume", "Put Vol", "int"),
           Col("option_volume", "Option Volume", "int"), Col("pc_volume_ratio", "P/C Vol", "ratio"),
           Col("total_premium", "Premium", "bigmoney"), Col("avg_iv30", "Avg IV30", "vol"),
           Col("avg_iv_rank", "Avg IV Rank", "pct"))


def _agg_row(name: str, f, m: np.ndarray) -> dict:
    cv, pv = np.nansum(f["call_volume"][m]), np.nansum(f["put_volume"][m])
    iv, ivr = f["iv30"][m], f["iv_rank"][m]
    return {"group": name, "symbols": int(m.sum()), "call_volume": int(cv), "put_volume": int(pv),
            "option_volume": int(cv + pv), "pc_volume_ratio": float(pv / cv) if cv > 0 else None,
            "total_premium": float(np.nansum(f["total_premium"][m])),
            "avg_iv30": float(np.nanmean(iv)) if np.isfinite(iv).any() else None,
            "avg_iv_rank": float(np.nanmean(ivr)) if np.isfinite(ivr).any() else None}


def run_broad_view(p: RunParams) -> Output:
    f = frame()
    m = base_mask(f, p) & np.isfinite(f["option_volume"])
    grp = np.array([("ETFs" if k == "etf" else "Indices" if k == "index" else (s or "Unclassified"))
                    for k, s in zip(f["kind"], f["sector"])], dtype=object)
    rows = [_agg_row("All", f, m)]
    for g in sorted(set(grp[m])):
        rows.append(_agg_row(g, f, m & (grp == g)))
    end = today()
    s = metrics_repo.series_between(end - timedelta(days=365), end, ["call_volume", "put_volume"])
    hist = []
    if len(s["date"]):
        if p.scope is not None:
            keep = np.isin(s["symbol"], p.scope)
            s = {k: v[keep] for k, v in s.items()}
        d, inv = np.unique(s["date"], return_inverse=True)
        cv = np.bincount(inv, np.nan_to_num(s["call_volume"]), len(d))
        pv = np.bincount(inv, np.nan_to_num(s["put_volume"]), len(d))
        hist = [{"date": dd.astype("datetime64[D]").astype(object), "call_volume": int(a), "put_volume": int(b),
                 "pc_ratio": float(b / a) if a > 0 else None} for dd, a, b in zip(d, cv, pv)]
    return Output(rows, extra={"pc_history": hist}, asof=f.asof)


BROAD_VIEW = ReportDef(
    "options-broad-view", "Options Broad View", "Options",
    "Option volume, put/call ratio, premium, average IV30 and IV rank per sector and for the whole universe, "
    "plus the market put/call volume history (extra.pc_history).",
    (UNIVERSE_ONLY, *SCOPE), BV_COLS, run_broad_view, ("option_volume", "desc"),
    ("P/C history covers days collected since the first sweep.", C.NOTE_IV_RANK))


# ============================================================================ put protection
PP_COLS = (*C.DESC_SHORT, Col("price", "Price", "money", C.G_QUOTE), Col("expiry", "Expiry", "date"),
           Col("dte", "DTE", "int"), Col("strike", "Put strike", "money"),
           Col("otm_pct", "% OTM", "pct", description="How far the put strike is below spot: 1 − strike/spot (≥ the target)"),
           Col("put_bid", "Bid", "money"), Col("put_ask", "Ask", "money"), Col("put_mid", "Mid", "money"),
           Col("cost_pct", "Cost %", "pct", description="ask / spot"),
           Col("annualized_cost", "Annualized cost", "pct", description="cost % · 365 / DTE"),
           Col("iv", "IV", "vol", C.G_VOL), Col("delta", "Delta", "num2"), Col("oi", "OI", "int"),
           Col("iv_rank", "IV Rank", "pct", C.G_VOL), Col("next_earnings", "Next Earnings", "date", C.G_CAT),
           Col("earnings_before_expiry", "Earnings before expiry", "bool", C.G_CAT))


def run_put_protection(p: RunParams) -> Output:
    target, otm = p["target_dte"], p["otm_pct"]
    lo, hi = max(0, target - 30), target + 45
    chains = context.load_universe_chain_arrays(p.scope, "P", lo, hi, p["min_oi"], 0)
    f = frame()
    pos = f.index()
    allowed = None if p.scope is not None else set(f["symbol"][base_mask(f, p)])
    rows = []
    for ca in chains:
        if allowed is not None and ca.symbol not in allowed:
            continue
        # an out-of-the-money put at least `otm` below spot: strike ≤ spot · (1 − otm), the highest such strike
        ok = (ca.ask > 0) & np.isfinite(ca.ask) & (ca.strike <= ca.spot * (1 - otm) * (1 + 1e-9))
        if not ok.any():
            continue
        dtes = ca.exp_dte
        cand = np.unique(ca.exp_idx[ok])
        e = cand[np.argmin(np.abs(dtes[cand] - target))]
        rows_e = np.nonzero(ok & (ca.exp_idx == e))[0]
        j = rows_e[np.argmax(ca.strike[rows_e])]
        exp, dte, k = ca.expiries[e], int(dtes[e]), float(ca.strike[j])
        cost = ca.ask[j] / ca.spot
        i = pos.get(ca.symbol)
        ne = ca.next_earnings
        rows.append({
            "symbol": ca.symbol, "name": f["name"][i] if i is not None else None,
            "market_cap": _fv(f["market_cap"][i]) if i is not None else None,
            "sector": f["sector"][i] if i is not None else None, "price": ca.spot, "expiry": exp, "dte": dte,
            "strike": k, "otm_pct": 1 - k / ca.spot, "put_bid": _fv(ca.bid[j]), "put_ask": _fv(ca.ask[j]),
            "put_mid": _fv(ca.mid[j]), "cost_pct": _fv(cost), "annualized_cost": _fv(cost * 365 / dte) if dte > 0 else None,
            "iv": _fv(ca.iv[j]), "delta": _fv(ca.delta[j]), "oi": int(ca.oi[j]), "iv_rank": ca.iv_rank,
            "next_earnings": ne, "earnings_before_expiry": bool(ne is not None and ca.asof_date <= ne <= exp)})
    return Output(rows, asof=f.asof)


def _fv(x) -> float | None:
    x = float(x)
    return x if np.isfinite(x) else None


PUT_PROTECTION = ReportDef(
    "put-protection", "Protective Put Cost", "Options",
    "Cost of a protective put near the target DTE and % out of the money for each symbol (ask as % of spot, "
    "annualized).",
    (p_int("target_dte", "Target DTE", 30, 1, 400), p_num("otm_pct", "% OTM", 0.05, 0, 0.5, step=0.01),
     p_int("min_oi", "Min OI", 0, 0), KIND, UNIVERSE_ONLY, *SCOPE),
    PP_COLS, run_put_protection, ("annualized_cost", "asc"),
    ("Uses the listed expiry nearest the target DTE and the highest listed strike at or below spot × (1 − % OTM), "
     "so the put is at least that far out of the money (symbols without one are left out); cost at the ask.",
     C.NOTE_CHAIN_UNIVERSE))

REPORTS = (UOV, IV_RANKINGS, IV_MOVERS, VOLUME_LEADERS, OI_CHANGES, LARGE_VOLUME, OPTION_SCREENER, BROAD_VIEW,
           PUT_PROTECTION)
