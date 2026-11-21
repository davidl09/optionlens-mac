"""Engine-computed IV and greeks for contracts the chain source sent none for (docs/INTEGRATION.md §6
"Computed IV and greeks").

A contract without a source IV gets one by Black-Scholes inversion (analytics.bs.implied_vol) of
- its mid, when bid and ask are both > 0 ("computed_mid"), else
- its last price, only when that trade is from the snapshot's session ("computed_last"): `last_ts` at/after
  the session's open, or, with no trade time, a session volume > 0.
A price outside the no-arbitrage bounds stays without IV (None). A contract without a source delta gets delta,
gamma, theta and vega from its IV (source or computed) via analytics.bs ("computed"). Source values are never
replaced; `theo` is never computed. Every value says where it came from (`iv_source`, `greeks_source`).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Iterable

from . import bs
from .calendar import NY, year_fraction_to_expiry

OptionContract = Any   # a models.OptionContract-like frozen dataclass (expiry, strike, right, bid, ask, last, ...)

COMPUTED_MID = "computed_mid"
COMPUTED_LAST = "computed_last"
COMPUTED = "computed"


def is_computed(source: str | None) -> bool:
    """Whether an iv_source / greeks_source names an engine computation (not the chain source)."""
    return source is not None and source.startswith(COMPUTED)


def traded_in_session(c: OptionContract, session_start: datetime) -> bool:
    """The contract's last price is from the session that starts at `session_start`."""
    if c.last_ts is not None:
        return c.last_ts >= session_start
    return (c.volume or 0) > 0


def model_price(c: OptionContract, session_start: datetime) -> tuple[float | None, str | None]:
    """(price to invert, iv_source label): the mid when both sides are quoted, else a current last."""
    if c.bid is not None and c.ask is not None and c.bid > 0 and c.ask > 0:
        return (c.bid + c.ask) / 2.0, COMPUTED_MID
    if c.last is not None and c.last > 0 and traded_in_session(c, session_start):
        return c.last, COMPUTED_LAST
    return None, None


def fill_iv_greeks(contracts: Iterable[OptionContract], *, spot: float | None, asof: datetime, r: float, q: float,
                   session_start: datetime, source: str | None) -> list[OptionContract]:
    """`contracts` with source values labelled `source` and missing IV / greeks computed (see module doc).
    `asof`: when the prices were current (the session's close for an after-hours or weekend snapshot); `spot`:
    the underlying price at that time."""
    asof_day = asof.astimezone(NY).date()
    out = []
    for c in contracts:
        iv, iv_src = (c.iv, source) if c.iv else (None, None)
        greeks_src = source if c.delta is not None else None
        live = spot is not None and spot > 0 and c.expiry >= asof_day
        t = year_fraction_to_expiry(c.expiry, asof) if live else None
        if iv is None and live:
            price, how = model_price(c, session_start)
            iv = bs.implied_vol(price, spot, c.strike, t, r, q, c.right) if price is not None else None
            iv_src = how if iv is not None else None
        g: dict = {}
        if c.delta is None and iv is not None and live:
            g = bs.greeks(spot, c.strike, t, r, q, iv, c.right)
            greeks_src = COMPUTED
        out.append(replace(
            c, iv=iv, iv_source=iv_src, greeks_source=greeks_src,
            **({k: float(g[k]) for k in ("delta", "gamma", "theta", "vega")} if g else {})))
    return out
