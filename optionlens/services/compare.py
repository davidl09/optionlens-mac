"""Multi-symbol volatility comparison (CompareVolatility)."""
from __future__ import annotations

from datetime import timedelta

from ..analytics import stats, vol
from ..repo import metrics as metrics_repo
from . import context as ctx
from .errors import BadRequest

MAX_SYMBOLS = 12
DAYS = 366


def _one(symbol: str) -> tuple[dict, dict]:
    sym = ctx.ensure_known(symbol)["symbol"]
    start = ctx.today() - timedelta(days=DAYS)
    a = stats.bar_arrays(ctx.load_bars(sym, start - timedelta(days=45)))
    hv20 = vol.hv_series(a.close, 20)
    iv = ctx.metrics_series(sym, ["iv30"])["iv30"]
    points = [{"date": d, "iv30": iv.get(d), "hv20": ctx.num(hv20[i])} for i, d in enumerate(a.dates) if d >= start]
    m = metrics_repo.get_metrics(sym) or {}
    rank = ctx.iv_rank_info(sym)
    iv30, hv = ctx.num(m.get("iv30")), vol.hv(a.close, 20)
    row = {"symbol": sym, "iv30": iv30, "iv_rank": rank["iv_rank"], "iv_percentile": rank["iv_percentile"],
           "hv20": hv, "iv_hv_ratio": iv30 / hv if iv30 is not None and hv else None,
           "iv60": ctx.num(m.get("iv60")), "iv90": ctx.num(m.get("iv90"))}
    return {"symbol": sym, "points": points}, row


def volatility(symbols: list[str]) -> dict:
    syms = list(dict.fromkeys(s for s in symbols if s))
    if not 1 <= len(syms) <= MAX_SYMBOLS:
        raise BadRequest(f"1..{MAX_SYMBOLS} symbols required")
    parts = [_one(s) for s in syms]
    return {"symbols": [p[0]["symbol"] for p in parts], "series": [p[0] for p in parts],
            "table": [p[1] for p in parts]}
