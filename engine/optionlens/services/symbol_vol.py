"""Per-symbol volatility payloads (types.ts): Volatility, Skew, VolSeasonality."""
from __future__ import annotations

from datetime import date, timedelta

from ..analytics import chainmetrics as cm
from ..analytics import stats, vol
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from . import context as ctx
from .symbol_options import expiry_infos, parse_expiry, require_chain

TENORS = vol.CM_TENORS
WEEK, MONTH = 7, 30


def _row_on_or_before(dates: list[date], d: date) -> int | None:
    return next((j for j in range(len(dates) - 1, -1, -1) if dates[j] <= d), None)


def _constant_maturity(symbol: str) -> list[dict]:
    h = metrics_repo.history(symbol, cols=[f"iv{t}" for t in TENORS])
    dates = [ctx.as_date(d) for d in h["date"]]
    if not dates:
        return [{"tenor": t, "iv": None, "chg_1d": None, "chg_1w": None} for t in TENORS]
    last = len(dates) - 1
    prev = last - 1 if last >= 1 else None
    week = _row_on_or_before(dates, dates[last] - timedelta(days=WEEK))
    out = []
    for t in TENORS:
        s = h[f"iv{t}"]
        cur = ctx.num(s[last])
        chg = lambda i: (cur - ctx.num(s[i])) if i is not None and cur is not None and ctx.num(s[i]) is not None else None  # noqa: E731
        out.append({"tenor": t, "iv": cur, "chg_1d": chg(prev), "chg_1w": chg(week)})
    return out


def _past_term(symbol: str, days_back: int) -> list[dict] | None:
    """Term structure from the stored EOD chain about `days_back` calendar days ago (≤ that date)."""
    target = ctx.today() - timedelta(days=days_back)
    eod = [d for d in options_repo.eod_dates(symbol) if d <= target]
    if not eod or (target - eod[-1]).days > days_back:   # nothing stored near that date
        return None
    ca = ctx.eod_chain_arrays(symbol, eod[-1])
    return cm.term_structure(ca) if ca is not None else None


def volatility(symbol: str) -> dict:
    """Works without listed options too (realized vol only; IV fields null)."""
    sym = ctx.refresh(symbol)["symbol"]
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False)
    bars = ctx.load_bars(sym)
    a = stats.bar_arrays(bars)
    m = metrics_repo.get_metrics(sym) or {}
    rank = ctx.iv_rank_info(sym)
    ivs = ctx.metrics_series(sym, ["iv30", "iv60", "iv90"])
    hv10, hv20, hv60 = (vol.hv_series(a.close, n) for n in (10, 20, 60))
    fwd = vol.realized_forward(a.close, 21)
    history = [{"date": d, "iv30": ivs["iv30"].get(d), "iv60": ivs["iv60"].get(d), "iv90": ivs["iv90"].get(d),
                "hv10": ctx.num(hv10[i]), "hv20": ctx.num(hv20[i]), "hv60": ctx.num(hv60[i])}
               for i, d in enumerate(a.dates)]
    iv_dates = set(ivs["iv30"])
    risk = [{"date": d, "iv30": ivs["iv30"].get(d), "hv20": ctx.num(hv20[i]), "realized_fwd21": ctx.num(fwd[i])}
            for i, d in enumerate(a.dates) if d in iv_dates]
    return {
        "symbol": sym, "asof": ca.asof if ca else None,
        "iv30": ctx.num(m.get("iv30")), "iv_rank": rank["iv_rank"], "iv_percentile": rank["iv_percentile"],
        "history_days": rank["history_days"], "iv_high_52w": rank["iv_high_52w"], "iv_low_52w": rank["iv_low_52w"],
        "constant_maturity": _constant_maturity(sym),
        "term_today": ctx.derived(ca, "term", cm.term_structure) if ca else [],
        "term_1w": _past_term(sym, WEEK), "term_1m": _past_term(sym, MONTH),
        "hv": [{"window": n, "value": v} for n, v in zip(vol.HV_WINDOWS, vol.hv_snapshot(a.close).values())],
        "parkinson20": vol.parkinson(a.high, a.low, 20),
        "history": history, "risk_premium": risk,
    }


def _default_skew_expiry(ca) -> date:
    """Expiry nearest 30 DTE (DTE ≥ 1)."""
    cands = [(abs(int(d) - 30), e) for e, d in zip(ca.expiries, ca.exp_dte) if d >= 1] or \
            [(0, ca.expiries[-1])]
    return min(cands)[1]


def skew(symbol: str, expiry: str | None = None) -> dict:
    ca = require_chain(symbol)
    exp = parse_expiry(ca, expiry, _default_skew_expiry(ca))
    i = ca.expiries.index(exp)
    h = metrics_repo.history(ca.symbol, cols=["skew25_30", "put_skew_30"])
    return {
        "symbol": ca.symbol, "expiry": exp, "dte": int(ca.exp_dte[i]), "spot": ca.spot,
        "expiries": expiry_infos(ca),
        "points": cm.smile(ca, exp),
        "by_delta": vol.iv_by_delta(ca, i),
        "history": [{"date": ctx.as_date(d), "skew25_30": ctx.num(h["skew25_30"][j]),
                     "put_skew_30": ctx.num(h["put_skew_30"][j])} for j, d in enumerate(h["date"])],
    }


def vol_seasonality(symbol: str) -> dict:
    sym = ctx.ensure_known(symbol)["symbol"]
    iv30 = ctx.metrics_series(sym, ["iv30"])["iv30"]
    return {"symbol": sym, "months": stats.vol_seasonality(iv30, ctx.load_bars(sym, live=False), ctx.today())}

