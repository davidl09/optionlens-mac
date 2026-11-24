"""Per-symbol stock payloads (types.ts): SymbolSummary, Bar[], PriceAction, Seasonality, Dividends,
Events. Option / volatility / earnings / company payloads live in the sibling symbol_* modules."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from ..analytics import dividends as divs_a
from ..analytics import markets, stats, vol
from ..models import normalize_symbol
from ..repo import dividends as div_repo
from ..repo import earnings as earn_repo
from ..repo import metrics as metrics_repo
from . import context as ctx
from . import fundamentals as fundamentals_svc
from . import symbol_earnings, symbol_options
from .errors import BadRequest

RANGES = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "3y": 365 * 3 + 1, "5y": 365 * 5 + 2,
          "10y": 365 * 10 + 3, "max": None}
SPY = "SPY"


def range_start(rng: str) -> date | None:
    if rng not in RANGES:
        raise BadRequest(f"range must be one of {', '.join(RANGES)}")
    days = RANGES[rng]
    return None if days is None else ctx.today() - timedelta(days=days)


# ----------------------------------------------------------------------------- bars / price action
SMA_WINDOWS = (20, 50, 200)


def bars(symbol: str, rng: str = "1y") -> list[dict]:
    """Daily bars of the range, each with sma20/sma50/sma200 computed over the full stored history (so a 1-month
    range still shows the 200-day average; null where fewer bars precede it)."""
    start = range_start(rng)
    ctx.ensure_known(symbol)
    all_bars = ctx.load_bars(symbol)
    closes = np.array([b.close for b in all_bars], dtype=float)
    smas = {w: stats.sma_series(closes, w) for w in SMA_WINDOWS}
    return [{"date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume or 0,
             **{f"sma{w}": ctx.num(smas[w][i]) for w in SMA_WINDOWS}}
            for i, b in enumerate(all_bars) if start is None or b.date >= start]


def price_action(symbol: str, rng: str = "1y") -> dict:
    start = range_start(rng)
    ctx.ensure_known(symbol)
    return stats.price_action(ctx.load_bars(symbol), start)


def seasonality(symbol: str) -> dict:
    sym = ctx.ensure_known(symbol)["symbol"]
    return {"symbol": sym, **stats.seasonality(ctx.load_bars(sym, live=False), ctx.today())}


# ----------------------------------------------------------------------------- dividends / events
def dividends(symbol: str) -> dict:
    """Dividends (types.ts). A Canadian listing has no dividend history (`unavailable` says why): what is shown is the
    current dividend Questrade reports."""
    sym = ctx.ensure_known(symbol)["symbol"]
    bars_ = ctx.load_bars(sym, live=False)
    q = ctx.quote_dict(sym)   # the price every page's dividend yield uses (key stats too)
    price = q["price"] if q else None
    out = divs_a.dividend_summary(sym, div_repo.for_symbol(sym), bars_, price, ctx.today())
    gap = ctx.nasdaq_gap(sym, "Dividend histories")
    return {**out, "unavailable": gap and gap + " Shown: the current dividend Questrade reports, when it reports one."}


EVENT_HISTORY = 8      # past earnings / ex-dates shown
EVENT_EXPIRIES = 12    # upcoming option expiries shown


def events(symbol: str) -> dict:
    sym = ctx.ensure_known(symbol)["symbol"]
    t = ctx.today()
    items = []
    earn = earn_repo.for_symbol(sym)
    past = [e for e in earn if e.date < t][-EVENT_HISTORY:]
    for e in past + [e for e in earn if e.date >= t]:
        items.append({"date": e.date, "kind": "earnings", "past": e.date < t,
                      "label": f"Earnings ({e.timing.upper() if e.timing != 'unknown' else 'time n/a'})"
                               + (f" {e.fiscal_quarter}" if e.fiscal_quarter else "")})
    dv = div_repo.for_symbol(sym)
    for d in [d for d in dv if d.ex_date < t][-EVENT_HISTORY:] + [d for d in dv if d.ex_date >= t]:
        items.append({"date": d.ex_date, "kind": "ex_dividend", "past": d.ex_date < t,
                      "label": f"Ex-dividend ${d.amount:g}" + (f" ({d.kind})" if d.kind not in ("cash", None) else "")})
    ca = ctx.load_chain_arrays(sym)
    for e in (ca.expiries if ca else ())[:EVENT_EXPIRIES]:
        if e >= t:
            items.append({"date": e, "kind": "expiry", "past": False,
                          "label": "Option expiry (monthly)" if markets.calendar_for(sym).is_monthly_expiry(e)
                          else "Option expiry"})
    items.sort(key=lambda x: (x["date"], x["kind"]))
    return {"symbol": sym, "items": items}


# ----------------------------------------------------------------------------- key stats
def key_stats(symbol: str) -> dict:
    """KeyStats (types.ts): the symbol's fundamental figures at its current price, each with its source
    (services.fundamentals; the one source of these figures for every page)."""
    row = ctx.ensure_known(symbol)
    q = ctx.quote_dict(row["symbol"])
    price = q["price"] if q else None
    return {"symbol": row["symbol"], "price": price, **fundamentals_svc.for_symbol(row, price, ctx.today())}


# ----------------------------------------------------------------------------- summary
def _option_volume(symbol: str) -> dict:
    h = metrics_repo.history(symbol, cols=["call_volume", "put_volume", "call_oi", "put_oi"])
    out = {"call_volume": None, "put_volume": None, "avg_option_volume_20d": None, "avg_option_volume_days": 0,
           "rel_option_volume": None, "pc_volume_ratio": None, "total_oi": None}
    if not len(h["date"]):
        return out
    cv, pv = h["call_volume"][-1], h["put_volume"][-1]
    avg, days = stats.prior_option_volume_avg(h["call_volume"][:-1], h["put_volume"][:-1])
    today_tot = cv + pv
    out.update(call_volume=ctx.num(cv), put_volume=ctx.num(pv), avg_option_volume_20d=avg, avg_option_volume_days=days,
               rel_option_volume=float(today_tot / avg) if avg and np.isfinite(today_tot) else None,
               pc_volume_ratio=float(pv / cv) if cv and np.isfinite(pv) and cv > 0 else None,
               total_oi=ctx.num(h["call_oi"][-1] + h["put_oi"][-1]))
    return out


def _beta_corr(symbol: str, a: stats.BarArrays) -> tuple[float | None, float | None]:
    if symbol == SPY:
        return (1.0, 1.0) if len(a) > 20 else (None, None)
    spy = stats.bar_arrays(ctx.load_bars(SPY, ctx.today() - timedelta(days=400), live=False))
    return stats.beta_corr(a.dates, a.close, spy.dates, spy.close)


def beta_1y(symbol: str) -> float | None:
    """The symbol's 1-year beta vs SPY (daily log returns; None without enough bars). Portfolio beta weighting."""
    sym = normalize_symbol(symbol)
    return _beta_corr(sym, stats.bar_arrays(ctx.load_bars(sym, ctx.today() - timedelta(days=400), live=False)))[0]


def _iv_hv_1y(symbol: str, a: stats.BarArrays) -> list[dict]:
    iv = ctx.metrics_series(symbol, ["iv30"])["iv30"]
    hv20 = vol.hv_series(a.close, 20)
    lo = max(0, len(a) - 252)
    return [{"date": a.dates[i], "iv30": iv.get(a.dates[i]), "hv20": ctx.num(hv20[i])} for i in range(lo, len(a))]


def summary(symbol: str) -> dict:
    row = ctx.refresh(symbol)
    sym = row["symbol"]
    bars_ = ctx.load_bars(sym)
    a = stats.bar_arrays(bars_)
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False)
    rank = ctx.iv_rank_info(sym)
    iv30 = rank["iv30"]
    hv20 = vol.hv(a.close, 20)
    h52, l52 = stats.high_low(a.high, a.low, a.close, 252)
    h3, l3 = stats.high_low(a.high, a.low, a.close, 756)
    beta, corr = _beta_corr(sym, a)
    support, resistance = stats.support_resistance(a.high, a.low, a.close)
    spot = ca.spot if ca else (float(a.close[-1]) if len(a) else None)
    nxt = symbol_earnings.next_earnings(sym, ca, bars_)
    div_list = div_repo.for_symbol(sym)
    ideas = symbol_options.ideas(sym)["by_edge"][:3] if ca else []
    quote = ctx.quote_dict(sym, bars_)
    ks = {"symbol": sym, "price": quote["price"] if quote else None,
          **fundamentals_svc.for_symbol(row, quote["price"] if quote else None, ctx.today())}
    return {
        "symbol": sym, "name": row["name"] or sym, "kind": row["kind"], **markets.describe(sym),
        "sector": row["sector"],
        "industry": row["industry"], "market_cap": ks["market_cap"], "optionable": row["optionable"],
        "in_universe": bool(row["in_universe"]), "quote": quote, "key_stats": ks,
        "avg_volume_30d": stats.avg_volume(a.volume, 30, exclude_last=True),
        "rel_volume": stats.relative_volume(a.volume, 30),
        # the 52-week range is a key stat (Questrade's) when there is one; else from the stored daily bars
        "high_52w": ks["high_52w"] if ks["high_52w"] is not None else h52,
        "low_52w": ks["low_52w"] if ks["low_52w"] is not None else l52, "high_3y": h3, "low_3y": l3,
        "beta_1y": beta, "corr_spy_1y": corr,
        "iv30": iv30, "iv30_asof": rank["iv30_asof"], "iv_rank": rank["iv_rank"], "iv_percentile": rank["iv_percentile"],
        "iv_history_days": rank["history_days"],
        "hv20": hv20, "iv_hv_ratio": iv30 / hv20 if iv30 is not None and hv20 else None,
        **_option_volume(sym),
        "returns": stats.returns_row(a.dates, a.close) if len(a) else dict.fromkeys(
            ("d1", "d5", "m1", "m3", "m6", "ytd", "y1", "y3")),
        "next_earnings": {k: nxt[k] for k in ("date", "timing", "days", "implied_move_pct", "avg_abs_move_pct")}
        if nxt else None,
        "next_dividend": divs_a.next_dividend(div_list, ks["price"] or spot, ctx.today()),   # the key stats' price
        "support": support, "resistance": resistance,
        "rsi14": stats.rsi(a.close, 14), "atr14_pct": stats.atr_pct(a.high, a.low, a.close, 14),
        "sma20": stats.sma(a.close, 20), "sma50": stats.sma(a.close, 50), "sma200": stats.sma(a.close, 200),
        "price_1y": [{"date": d, "close": float(c)} for d, c in zip(a.dates[-252:], a.close[-252:])],
        "iv_hv_1y": _iv_hv_1y(sym, a),
        "ideas": ideas,
        "options_asof": ca.asof if ca else None,
    }
