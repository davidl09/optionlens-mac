"""MarketOverview (dashboard, SPEC §9): index cards, VIX term structure + 1y history, sector ETF
strip, and the top rows of the movers / unusual volume / IV movers / earnings / watchlist reports
(rows keep each report's column keys so the UI renders them with that report's ColumnDefs)."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

import numpy as np

from ..analytics.calendar import NY, next_trading_day
from ..repo import market as market_repo
from ..repo import quotes as quotes_repo
from ..repo import user as user_repo
from . import context, user
from .reports import registry, universe
from .reports.base import jsonable, today
from .reports.common import bar_lists

INDEX_CARDS = (("SPY", "SPDR S&P 500 ETF"), ("QQQ", "Invesco QQQ"), ("IWM", "iShares Russell 2000"),
               ("DIA", "SPDR Dow Jones"), ("SPX", "S&P 500 Index"), ("NDX", "Nasdaq-100 Index"),
               ("RUT", "Russell 2000 Index"))
SECTOR_ETFS = (("XLK", "Technology"), ("XLF", "Financials"), ("XLE", "Energy"), ("XLV", "Health Care"),
               ("XLY", "Consumer Discretionary"), ("XLP", "Consumer Staples"), ("XLI", "Industrials"),
               ("XLB", "Materials"), ("XLU", "Utilities"), ("XLRE", "Real Estate"), ("XLC", "Communication Services"))
VIX_TENORS = (("VIX9D", "vix9d"), ("VIX", "vix"), ("VIX3M", "vix3m"), ("VIX6M", "vix6m"))
SPARK_DAYS = 30
TOP_N = 10
CACHE_TTL_S = 60

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def overview() -> dict:
    """MarketOverview (cached CACHE_TTL_S; the underlying frame is cached per sweep)."""
    with _lock:
        hit = _cache.get("overview")
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
    out = jsonable(_build())
    with _lock:
        _cache["overview"] = (time.monotonic(), out)
    return out


def _val(f, i, k):
    if i is None:
        return None
    v = f[k][i]
    return None if isinstance(v, float) and v != v else (v.item() if isinstance(v, np.generic) else v)


def _panel(report_id: str, query: dict | None = None, n: int = TOP_N) -> tuple[list[dict], str]:
    """(top n rows, why-empty text): the report's run-time notes (not its static limitations), else a
    generic 'no row matches' line."""
    res = registry.run_sync(report_id, query or {})
    static = set(registry.get(report_id).notes)
    why = " ".join(x for x in res["notes"] if x not in static)
    return res["rows"][:n], why or f"No rows match the {res['title']} report's default criteria right now."


def _earnings_note(day, label: str, any_events: bool) -> str:
    if not any_events:
        return "No upcoming earnings events stored (the calendar job collects today−10 … today+90 daily)."
    return f"No tracked company reports {label} ({day.isoformat()})."


def _build() -> dict:
    f = universe.get_frame()
    pos = f.index()
    t = today()
    spark = bar_lists([s for s, _ in INDEX_CARDS], t - timedelta(days=SPARK_DAYS * 2 + 10))
    indices = []
    for sym, label in INDEX_CARDS:
        i = pos.get(sym)
        indices.append({"symbol": sym, "name": _val(f, i, "name") or label, "price": _val(f, i, "price"),
                        "change_pct": _val(f, i, "change_pct"), "iv30": _val(f, i, "iv30"),
                        "spark": [b.close for b in spark.get(sym, [])[-SPARK_DAYS:]]})
    sectors = [{"symbol": s, "name": label, "change_pct": _val(f, pos.get(s), "change_pct"),
                "change_5d": _val(f, pos.get(s), "ret_5d"), "change_1m": _val(f, pos.get(s), "ret_1m")}
               for s, label in SECTOR_ETFS]
    tom = next_trading_day(t)
    cal = registry.run_sync("earnings-calendar", {"start": t.isoformat(), "end": tom.isoformat()})["rows"]
    wls = user_repo.list_watchlists()
    panels = {
        "gainers": _panel("market-movers", {"side": "gainers"}), "losers": _panel("market-movers", {"side": "losers"}),
        "unusual_options": _panel("unusual-option-volume"), "iv_movers": _panel("iv-movers"),
    }
    any_events = bool(context.next_earnings_map())
    panels["earnings_today"] = ([r for r in cal if r["date"] == t.isoformat()][:15],
                                _earnings_note(t, "today", any_events))
    panels["earnings_tomorrow"] = ([r for r in cal if r["date"] == tom.isoformat()][:15],
                                   _earnings_note(tom, "on the next session", any_events))
    panels["watchlist"] = (user.watchlist_table(wls[0]["id"])["rows"] if wls else [],
                           "Empty watchlist." if wls else "No watchlist yet — create one under My › Watchlists.")
    return {
        "asof": f.asof, "indices": indices, **_vix(), "sectors": sectors,
        **{k: rows for k, (rows, _) in panels.items()},
        "empty_notes": {k: why for k, (rows, why) in panels.items() if not rows},
    }


def _vix() -> dict:
    """vix_term (latest value + change vs prior close; a stored live quote wins when newer) and vix_history (1y)."""
    t = today()
    bars = market_repo.index_bars([s for s, _ in VIX_TENORS], t - timedelta(days=366))
    quotes = quotes_repo.all_quotes()
    term = []
    for sym, _ in VIX_TENORS:
        b = bars.get(sym)
        value = change = None
        if b is not None and len(b["close"]):
            c = b["close"]
            value = float(c[-1])
            change = float(c[-1] - c[-2]) if len(c) > 1 else None
            last_d = b["date"][-1].astype("datetime64[D]").astype(object)
        else:
            last_d = None
        q = quotes.get(sym)
        if q is not None and q.price and (last_d is None or q.ts.astimezone(NY).date() > last_d):
            value, change = float(q.price), q.change
        term.append({"tenor": sym, "value": value, "change": change})
    by_date: dict = {}
    for sym, key in VIX_TENORS:
        b = bars.get(sym)
        if b is None:
            continue
        for d, c in zip(b["date"], b["close"].tolist()):
            by_date.setdefault(d.astype("datetime64[D]").astype(object), {})[key] = c
    hist = [{"date": d, **{k: v.get(k) for _, k in VIX_TENORS}} for d, v in sorted(by_date.items())]
    return {"vix_term": term, "vix_history": hist}


def invalidate(*_args) -> None:
    with _lock:
        _cache.clear()
