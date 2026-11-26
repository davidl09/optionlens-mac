"""The per-sweep "universe frame": one row per symbol, columns as numpy arrays, built from bulk repo
reads (symbols, quotes, daily bars, metrics history, earnings and dividend maps). Every stock-level
report, the market overview, watchlist tables and alert evaluation derive from it with vectorized
ops. Cached until the next sweep/EOD completes (``invalidate`` is registered on the job hooks) or
FRAME_TTL_S elapses.

Frame rows: universe symbols ∪ symbols with a stored quote (on-demand visits) ∪ watchlist symbols.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

import numpy as np

from ...analytics import markets, stats, vol
from ...analytics.calendar import NY, now_ny
from ...repo import bars as bars_repo
from ...repo import dividends as div_repo
from ...repo import earnings as earn_repo
from ...repo import metrics as metrics_repo
from ...repo import quotes as quotes_repo
from ...repo import symbols as symbols_repo
from ...repo import user as user_repo
from .. import context
from .. import fundamentals as fundamentals_svc

FRAME_TTL_S = 900
BARS_LOOKBACK_DAYS = 400          # calendar days of bars (52w highs, SMA200, 1y return, beta)
HISTORY_ROWS = vol.IV_RANK_ROWS   # stored metrics rows per symbol (IV rank window)
SENTIMENT_THRESHOLD = 0.70
BENCH = "SPY"
_QUOTE_COLS = ("price", "change", "change_pct", "prev_close", "open", "high", "low", "volume", "bid", "ask", "vwap")

_TEXT = ("symbol", "market", "currency", "name", "kind", "sector", "industry", "sentiment", "earnings_timing")
_DATES = ("next_earnings", "last_earnings", "next_ex_div", "last_bar_date", "metrics_date")


@dataclass
class Frame:
    day: date                         # New York calendar date of the build
    metrics_date: date | None         # latest stored metrics date (today's sweep date when swept)
    asof: datetime | None             # newest quote timestamp
    cols: dict[str, np.ndarray]
    iv_history_days: int              # distinct stored metrics dates (max over symbols)
    opt_history_days: int             # prior stored days in the avg option volume (max over symbols, ≤ 20)
    built_at: float

    def __len__(self) -> int:
        return len(self.cols["symbol"])

    def __getitem__(self, key: str) -> np.ndarray:
        return self.cols[key]

    def index(self) -> dict[str, int]:
        return {s: i for i, s in enumerate(self.cols["symbol"])}

    def select(self, symbols: Iterable[str] | None) -> np.ndarray:
        """Boolean mask of rows whose symbol is in `symbols` (all rows when None)."""
        if symbols is None:
            return np.ones(len(self), bool)
        return np.isin(self.cols["symbol"], list(symbols))

    def rows(self, idx: Sequence[int] | np.ndarray, keys: Iterable[str]) -> list[dict]:
        """Row dicts for row indices (in order) with the given keys (NaN -> None)."""
        idx = np.asarray(idx, dtype=int)
        lists = {}
        for k in keys:
            a = self.cols[k][idx]
            if a.dtype.kind == "f":
                lists[k] = [None if x != x else x for x in a.tolist()]
            else:
                lists[k] = a.tolist()
        ks = list(lists)
        return [{k: lists[k][i] for k in ks} for i in range(len(idx))]


_lock = threading.Lock()
_cache: dict[str, Frame | None] = {"frame": None}


def invalidate(*_args) -> None:
    """Drop the cached frame (hook signature fn(day, symbols) accepted)."""
    _cache["frame"] = None


def get_frame(max_age_s: float = FRAME_TTL_S) -> Frame:
    """Cached universe frame (rebuilt when invalidated, older than max_age_s, or the date changed)."""
    f = _cache["frame"]
    if f is not None and time.monotonic() - f.built_at < max_age_s and f.day == now_ny().date():
        return f
    with _lock:
        f = _cache["frame"]
        if f is not None and time.monotonic() - f.built_at < max_age_s and f.day == now_ny().date():
            return f
        f = build_frame()
        _cache["frame"] = f
        return f


def in_watchlist_mask(frame: Frame) -> np.ndarray:
    """Per-request (not cached): symbol is in any watchlist."""
    return frame.select(set(user_repo.watchlist_symbols()))


# ----------------------------------------------------------------------------- build
def _nan(n: int) -> np.ndarray:
    return np.full(n, np.nan)


def _obj(n: int) -> np.ndarray:
    return np.full(n, None, dtype=object)


def build_frame(today: date | None = None) -> Frame:
    """Build the frame from bulk repo reads (see module doc)."""
    now = now_ny()
    today = today or now.date()
    quotes = quotes_repo.all_quotes()
    wl = set(user_repo.watchlist_symbols())
    syms_rows = [r for r in symbols_repo.list_symbols()
                 if r["in_universe"] or r["symbol"] in quotes or r["symbol"] in wl]
    syms = [r["symbol"] for r in syms_rows]
    n = len(syms)
    pos = {s: i for i, s in enumerate(syms)}
    C: dict[str, np.ndarray] = {}

    # ---- description
    C["symbol"] = np.array(syms, dtype=object)
    C["market"] = np.array([markets.market_of(s).code for s in syms], dtype=object)
    C["currency"] = np.array([markets.currency_of(s) for s in syms], dtype=object)
    for k in ("name", "kind", "sector", "industry"):
        C[k] = np.array([r.get(k) for r in syms_rows], dtype=object)
    C["market_cap"] = np.array([np.nan if r.get("market_cap") is None else r["market_cap"] for r in syms_rows], float)
    C["in_universe"] = np.array([bool(r["in_universe"]) for r in syms_rows], dtype=bool)
    C["optionable"] = np.array([r.get("optionable") is not False for r in syms_rows], dtype=bool)

    # ---- quote
    for k in _QUOTE_COLS:
        C[k] = _nan(n)
    C["quote_ts"] = _obj(n)
    asof = None
    for s, q in quotes.items():
        i = pos.get(s)
        if i is None:
            continue
        for k in _QUOTE_COLS:
            v = getattr(q, k)
            if v is not None:
                C[k][i] = float(v)
        C["quote_ts"][i] = q.ts
        asof = q.ts if asof is None or q.ts > asof else asof
    _bars_block(C, syms, pos, quotes, today)
    mdate = _metrics_block(C, syms, pos)
    _events_block(C, syms, pos, today)
    _fundamentals_block(C, syms, pos, today)
    _halts_block(C, syms, pos)
    iv_days = int(np.nanmax(C["iv_history_days"])) if n and np.isfinite(C["iv_history_days"]).any() else 0
    opt_days = int(np.nanmax(C["opt_history_days"])) if n and np.isfinite(C["opt_history_days"]).any() else 0
    return Frame(today, mdate, asof, C, iv_days, opt_days, time.monotonic())


def _bars_block(C: dict, syms: list[str], pos: dict, quotes: dict, today: date) -> None:
    n = len(syms)
    keys = ("ret_1d", "ret_5d", "ret_1m", "ret_3m", "ret_6m", "ret_ytd", "ret_1y", "avg_volume_30d", "rel_volume",
            "high_52w", "low_52w", "prior_high_52w", "prior_low_52w", "pct_from_high_52w", "pct_from_low_52w",
            "sma20", "sma50", "sma200", "pct_vs_sma20", "pct_vs_sma50", "pct_vs_sma200", "rsi14", "atr14_pct",
            "beta", "corr_spy", "gap_pct", "bar_days", "last_close")
    for k in keys:
        C[k] = _nan(n)
    C["last_bar_date"] = _obj(n)
    allbars = bars_repo.bars_since(today - timedelta(days=BARS_LOOKBACK_DAYS), syms)
    series: dict[str, tuple] = {}
    for s, b in allbars.items():
        series[s] = with_live_bar(b, quotes.get(s))
    bench = series.get(BENCH)
    for s, (dates, o, h, lo, c, v, live) in series.items():
        i = pos[s]
        if len(c) == 0:
            continue
        C["bar_days"][i] = len(c)
        C["last_bar_date"][i] = dates[-1]
        C["last_close"][i] = c[-1]
        rr = stats.returns_row(dates, c)
        for src, dst in (("d1", "ret_1d"), ("d5", "ret_5d"), ("m1", "ret_1m"), ("m3", "ret_3m"),
                         ("m6", "ret_6m"), ("ytd", "ret_ytd"), ("y1", "ret_1y")):
            if rr[src] is not None:
                C[dst][i] = rr[src]
        C["avg_volume_30d"][i] = _f(stats.avg_volume(v, 30, exclude_last=True))
        C["rel_volume"][i] = _f(stats.relative_volume(v, 30))
        hh, ll = stats.high_low(h, lo, c, 252)
        C["high_52w"][i], C["low_52w"][i] = _f(hh), _f(ll)
        if len(c) > 1:
            ph, pl = stats.high_low(h[:-1], lo[:-1], c[:-1], 252)
            C["prior_high_52w"][i], C["prior_low_52w"][i] = _f(ph), _f(pl)
        for w in (20, 50, 200):
            C[f"sma{w}"][i] = _f(stats.sma(c, w))
        C["rsi14"][i] = _f(stats.rsi(c, 14))
        C["atr14_pct"][i] = _f(stats.atr_pct(h, lo, c, 14))
        if len(c) > 1 and np.isfinite(o[-1]) and c[-2] > 0:
            C["gap_pct"][i] = o[-1] / c[-2] - 1.0
        if bench is not None and s != BENCH:
            b_, corr = stats.beta_corr(dates, c, bench[0], bench[4])
            C["beta"][i], C["corr_spy"][i] = _f(b_), _f(corr)
        elif s == BENCH:
            C["beta"][i], C["corr_spy"][i] = 1.0, 1.0
    # a quote older than the last stored bar (e.g. an on-demand visit long ago) is stale: use the bars
    stale = np.array([ts is not None and d is not None and ts.astimezone(NY).date() < d
                      for ts, d in zip(C["quote_ts"], C["last_bar_date"])], dtype=bool)
    for k in _QUOTE_COLS:
        C[k][stale] = np.nan
    # quote fields without a (fresh) quote value: the latest daily bar's (one rule with context.quote_dict)
    for s, (dates, o, h, lo, c, v, live) in series.items():
        i = pos[s]
        for k, val in stats.latest_bar_quote(o, h, lo, c, v).items():
            if val is not None and not np.isfinite(C[k][i]):
                C[k][i] = val
    # the quote is authoritative for the day's change; otherwise the last bar-to-bar return
    C["change_pct"] = np.where(np.isfinite(C["change_pct"]), C["change_pct"], C["ret_1d"])
    C["ret_1d"] = C["change_pct"].copy()
    with np.errstate(divide="ignore", invalid="ignore"):
        px = np.where(np.isfinite(C["price"]), C["price"], C["last_close"])
        C["pct_from_high_52w"] = px / C["high_52w"] - 1.0
        C["pct_from_low_52w"] = px / C["low_52w"] - 1.0
        for w in (20, 50, 200):
            C[f"pct_vs_sma{w}"] = px / C[f"sma{w}"] - 1.0
        C["pct_vs_vwap"] = C["price"] / C["vwap"] - 1.0


def with_live_bar(b: dict, q) -> tuple:
    """Bars as lists, with today's quote appended as a bar when it is newer than the last stored bar."""
    dates = [d.astype("datetime64[D]").item() for d in b["date"]]
    o, h, lo, c, v = (np.asarray(b[k], float) for k in ("open", "high", "low", "close", "volume"))
    live = False
    qd = context.quote_session(q) if q is not None and q.price else None
    if qd is not None:
        if not dates or qd > dates[-1]:
            dates = dates + [qd]
            o = np.append(o, np.nan if q.open is None else q.open)
            h = np.append(h, np.nan if q.high is None else q.high)
            lo = np.append(lo, np.nan if q.low is None else q.low)
            c = np.append(c, q.price)
            v = np.append(v, np.nan if q.volume is None else q.volume)
            live = True
    return dates, o, h, lo, c, v, live


_HIST_COLS = ("iv30", "hv30", "skew25_30", "call_volume", "put_volume")


def market_dates() -> dict[markets.Market, date]:
    """Each market's latest metrics date. Markets keep their own calendars: a TSX session on a US holiday (or the
    other way round) must not hide the other market's last session."""
    out: dict[markets.Market, date] = {}
    for s, d in metrics_repo.latest_dates().items():
        m = markets.market_of(s)
        if m not in out or d > out[m]:
            out[m] = d
    return out


def _metrics_block(C: dict, syms: list[str], pos: dict) -> date | None:
    n = len(syms)
    by_market = market_dates()
    mdate = max(by_market.values(), default=None)
    mcols = metrics_repo.metric_columns()
    for k in mcols:
        if k not in ("close", "change_pct", "volume"):
            C[k] = _nan(n)
    C["metrics_date"] = _obj(n)
    latest = {s: r for d in set(by_market.values()) for s, r in metrics_repo.metrics_on(d).items()
              if by_market.get(markets.market_of(s)) == d}   # each symbol's row of its market's latest session
    for s, row in latest.items():
        i = pos.get(s)
        if i is None:
            continue
        C["metrics_date"][i] = row["date"]
        for k in mcols:
            if k in ("close", "change_pct", "volume"):
                continue
            v = row.get(k)
            if v is not None:
                C[k][i] = float(v)
    derived = ("iv_rank", "iv_pct", "iv_history_days", "iv30_prev", "iv30_chg_1d", "iv30_chg_pct", "iv30_chg_5d",
               "avg_option_volume_20d", "opt_history_days", "skew_rank", "iv_hv_rank", "iv_high_52w", "iv_low_52w")
    for k in derived:
        C[k] = _nan(n)
    hist = metrics_repo.history_bulk(_HIST_COLS, last_n=HISTORY_ROWS, symbols=syms) if mdate else {}
    ranks = context.iv_rank_all() if mdate else {}
    for s, h in hist.items():
        i = pos.get(s)
        if i is None:
            continue
        sdate = by_market.get(markets.market_of(s))   # the symbol's market's latest session
        dts = h["date"]
        has_today = sdate is not None and len(dts) and dts[-1] == np.datetime64(sdate)
        iv = h["iv30"]
        r = ranks.get(s)   # the symbol page's IV rank (context.iv_history: collected, else ingested IV30)
        C["iv_history_days"][i] = r["history_days"] if r else int(np.isfinite(iv).sum())
        if has_today:
            if r:
                if r["iv30_asof"] == sdate:   # the value the rank is of (collected, else ingested IV30)
                    C["iv30"][i] = _f(r["iv30"])
                C["iv_rank"][i], C["iv_pct"][i] = _f(r["iv_rank"]), _f(r["iv_percentile"])
                C["iv_high_52w"][i], C["iv_low_52w"][i] = _f(r["iv_high_52w"]), _f(r["iv_low_52w"])
            prev = iv[:-1]
            if len(prev) and np.isfinite(prev[-1]) and np.isfinite(iv[-1]):
                C["iv30_prev"][i] = prev[-1]
                C["iv30_chg_1d"][i] = iv[-1] - prev[-1]
                C["iv30_chg_pct"][i] = iv[-1] / prev[-1] - 1.0 if prev[-1] > 0 else np.nan
            if len(iv) > 5 and np.isfinite(iv[-6]) and np.isfinite(iv[-1]):
                C["iv30_chg_5d"][i] = iv[-1] - iv[-6]
            sk = vol.iv_rank_percentile(h["skew25_30"].tolist())
            C["skew_rank"][i] = _f(sk["iv_rank"])
            prem = vol.iv_rank_percentile((iv - h["hv30"]).tolist())
            C["iv_hv_rank"][i] = _f(prem["iv_rank"])
            avg, days = stats.prior_option_volume_avg(h["call_volume"][:-1], h["put_volume"][:-1])
            C["opt_history_days"][i] = days
            if avg is not None:
                C["avg_option_volume_20d"][i] = avg
    cv, pv = C["call_volume"], C["put_volume"]
    with np.errstate(divide="ignore", invalid="ignore"):
        C["option_volume"] = np.where(np.isfinite(cv) | np.isfinite(pv), np.nan_to_num(cv) + np.nan_to_num(pv), np.nan)
        C["total_oi"] = np.where(np.isfinite(C["call_oi"]) | np.isfinite(C["put_oi"]),
                                 np.nan_to_num(C["call_oi"]) + np.nan_to_num(C["put_oi"]), np.nan)
        C["total_premium"] = np.where(np.isfinite(C["call_premium"]) | np.isfinite(C["put_premium"]),
                                      np.nan_to_num(C["call_premium"]) + np.nan_to_num(C["put_premium"]), np.nan)
        ov = C["option_volume"]
        C["pc_volume_ratio"] = np.where(cv > 0, pv / cv, np.nan)
        C["pc_oi_ratio"] = np.where(C["call_oi"] > 0, C["put_oi"] / C["call_oi"], np.nan)
        C["pct_call"] = np.where(ov > 0, cv / ov, np.nan)
        C["pct_put"] = np.where(ov > 0, pv / ov, np.nan)
        C["rel_option_volume"] = np.where(C["avg_option_volume_20d"] > 0, ov / C["avg_option_volume_20d"], np.nan)
        C["iv_hv_ratio"] = np.where(C["hv20"] > 0, C["iv30"] / C["hv20"], np.nan)
        C["iv_hv30_ratio"] = np.where(C["hv30"] > 0, C["iv30"] / C["hv30"], np.nan)
        C["iv_hv20_spread"] = C["iv30"] - C["hv20"]
        C["iv_hv30_spread"] = C["iv30"] - C["hv30"]
        C["term_slope"] = C["iv90"] - C["iv30"]
        C["term_ratio"] = np.where(C["iv90"] > 0, C["iv30"] / C["iv90"], np.nan)
    C["backwardation"] = np.isfinite(C["iv30"]) & np.isfinite(C["iv90"]) & (C["iv30"] > C["iv90"] * 1.05)
    sent = np.full(n, None, dtype=object)
    sent[np.isfinite(C["pct_call"])] = "Neutral"
    sent[C["pct_call"] >= SENTIMENT_THRESHOLD] = "Bullish"
    sent[C["pct_put"] >= SENTIMENT_THRESHOLD] = "Bearish"
    C["sentiment"] = sent
    return mdate


def _events_block(C: dict, syms: list[str], pos: dict, today: date) -> None:
    n = len(syms)
    for k in ("next_earnings", "earnings_timing", "last_earnings", "next_ex_div"):
        C[k] = _obj(n)
    for k in ("days_to_earnings", "days_since_earnings", "next_div_amount", "days_to_ex_div", "div_ttm", "div_yield"):
        C[k] = _nan(n)
    for s, ev in earn_repo.next_events(today).items():
        i = pos.get(s)
        if i is not None:
            C["next_earnings"][i] = ev.date
            C["earnings_timing"][i] = ev.timing
            C["days_to_earnings"][i] = (ev.date - today).days
    for s, ev in earn_repo.last_events(today).items():
        i = pos.get(s)
        if i is not None:
            C["last_earnings"][i] = ev.date
            C["days_since_earnings"][i] = (today - ev.date).days
    for s, d in div_repo.next_ex(today).items():
        i = pos.get(s)
        if i is not None:
            C["next_ex_div"][i] = d.ex_date
            C["next_div_amount"][i] = d.amount
            C["days_to_ex_div"][i] = (d.ex_date - today).days
    for s, amt in div_repo.trailing_sum(today).items():
        i = pos.get(s)
        if i is not None:
            C["div_ttm"][i] = amt


_FUND_NUM = ("pe", "eps", "shares_outstanding", "avg_volume_20d", "avg_volume_3m")
_FUND_TEXT = {"industry_sector": "sector", "industry_group": "industry_group", "industry_subgroup": "industry_subgroup"}


def _fundamentals_block(C: dict, syms: list[str], pos: dict, today: date) -> None:
    """Key stats (services.fundamentals: the one source of these figures) at the frame's price: market cap (replacing
    the symbols table's), P/E, EPS, shares, average volumes, Questrade's classification, the dividend yield."""
    n = len(syms)
    px = np.where(np.isfinite(C["price"]), C["price"], C["last_close"])
    prices = {s: (float(px[i]) if np.isfinite(px[i]) else None) for s, i in pos.items()}
    caps = {s: (float(C["market_cap"][i]) if np.isfinite(C["market_cap"][i]) else None) for s, i in pos.items()}
    stats_ = fundamentals_svc.for_symbols(prices, caps, today)
    for k in (*_FUND_NUM, "market_cap", "div_yield"):
        C[k] = _nan(n)
    for k in (*_FUND_TEXT, "fundamentals_source"):
        C[k] = _obj(n)
    for s, st in stats_.items():
        i = pos[s]
        for k in _FUND_NUM:
            C[k][i] = _f(st[k])
        C["market_cap"][i] = _f(st["market_cap"])
        C["div_yield"][i] = _f(st["dividend_yield"])
        for k, src in _FUND_TEXT.items():
            C[k][i] = st[src]
        C["fundamentals_source"][i] = st["sources"].get("pe") or st["sources"].get("market_cap")


def _halts_block(C: dict, syms: list[str], pos: dict) -> None:
    """halted: True / False from a fresh halt observation (context.halt_flags), None = unknown."""
    C["halted"] = _obj(len(syms))
    for s, h in context.halt_flags(syms).items():
        if s in pos:
            C["halted"][pos[s]] = h["halted"]


def _f(x) -> float:
    return np.nan if x is None else float(x)
