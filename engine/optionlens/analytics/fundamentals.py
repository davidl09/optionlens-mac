"""Key stats (pure): the one resolution of a symbol's fundamental figures (symbol header, Summary, peers, screeners;
docs/INTEGRATION.md §8 "Reference data").

The day's reference snapshot (Questrade GET v1/symbols/:id) is the single source of P/E, EPS, market cap, shares
outstanding, the 52-week range, the 20-day / 3-month average volumes and Questrade's sector / industry group /
sub-group. P/E and market cap follow the current price: price / EPS (positive EPS only) and price × shares, the same
formula the app applies to live prices; without a price, the snapshot's own values. Without a snapshot only the market
cap is known (the symbols table's, from Nasdaq's screener). The dividend yield is the indicated yield of the merged
dividends (analytics.dividends.indicated_yield), else the snapshot's (Questrade: the current dividend annualized).
Every figure says where it came from (`sources`).
"""
from __future__ import annotations

from datetime import date

from ..models import Fundamentals

SNAPSHOT_FIGURES = ("eps", "shares_outstanding", "high_52w", "low_52w", "avg_volume_20d", "avg_volume_3m", "sector",
                    "industry_group", "industry_subgroup")
FIGURES = ("pe", "market_cap", *SNAPSHOT_FIGURES, "dividend_yield")


def live_pe(price: float | None, eps: float | None) -> float | None:
    return price / eps if price and price > 0 and eps and eps > 0 else None


def live_market_cap(price: float | None, shares: float | None) -> float | None:
    return price * shares if price and price > 0 and shares and shares > 0 else None


def key_stats(snap: Fundamentals | None, price: float | None, fallback_market_cap: float | None,
              indicated_yield: tuple[float | None, str | None] = (None, None)) -> dict:
    """{<FIGURES>..., asof (the snapshot's date), sources: {figure: source}} (module doc)."""
    out: dict = dict.fromkeys(FIGURES)
    src: dict[str, str] = {}
    if snap is not None:
        for k in SNAPSHOT_FIGURES:
            out[k] = getattr(snap, k)
        out["pe"] = live_pe(price, snap.eps) if price else snap.pe
        out["market_cap"] = live_market_cap(price, snap.shares_outstanding) if price else snap.market_cap
        if out["market_cap"] is None:
            out["market_cap"] = snap.market_cap
        src.update({k: snap.source for k in FIGURES if out[k] is not None})
    if out["market_cap"] is None and fallback_market_cap:
        out["market_cap"], src["market_cap"] = fallback_market_cap, "nasdaq"
    y, y_src = indicated_yield
    if y is not None:
        out["dividend_yield"], src["dividend_yield"] = y, y_src or "computed"
    elif snap is not None and snap.dividend_yield is not None:
        out["dividend_yield"], src["dividend_yield"] = snap.dividend_yield, snap.source
    asof: date | None = snap.date if snap is not None else None
    return {**out, "asof": asof, "sources": src}
