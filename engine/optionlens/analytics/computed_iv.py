"""Engine-computed IV and greeks for contracts the chain source sent none for (docs/INTEGRATION.md §6
"Computed IV and greeks").

A contract without a source IV gets one by Black-Scholes inversion (analytics.bs.implied_vol) of
- its mid, when bid and ask are both > 0 ("computed_mid"), else
- its last price, only when that trade is from the snapshot's session ("computed_last"): `last_ts` at/after
  the session's open, or, with no trade time, a session volume > 0.
A price outside the no-arbitrage bounds stays without IV (None). A contract without a source delta gets delta,
gamma, theta and vega from its IV (source or computed) via analytics.bs ("computed"). Source values are never
replaced; `theo` is never computed. Every value says where it came from (`iv_source`, `greeks_source`).

Before computing, `borrow_source_iv` gives a contract without a source IV the IV (and greeks) another chain source
sent for the same contract in the same session, labelled with that source: the engine computes only where no
source has an IV.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Iterable, Mapping

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


GREEKS = ("delta", "gamma", "theta", "vega")


def borrow_source_iv(contracts: Iterable[OptionContract], stored: Mapping[str, Mapping],
                     source: str | None) -> list[OptionContract]:
    """Contracts without a source IV take it from `stored` ({osi: {iv, delta, gamma, theta, vega, iv_source,
    greeks_source, source}}: another source's source-given values of the same session), and its greeks when they
    have none, keeping that source's labels. Rows of `source` itself are not borrowed."""
    out = []
    for c in contracts:
        s = stored.get(c.osi)
        if c.iv or s is None or s.get("source") == source or not s.get("iv"):
            out.append(c)
            continue
        greeks = {k: s[k] for k in GREEKS} if c.delta is None and s.get("delta") is not None else {}
        out.append(replace(c, iv=s["iv"], iv_source=s["iv_source"],
                           **({**greeks, "greeks_source": s["greeks_source"]} if greeks else {})))
    return out


def fill_iv_greeks(contracts: Iterable[OptionContract], *, spot: float | None, asof: datetime, r: float, q: float,
                   session_start: datetime, source: str | None) -> list[OptionContract]:
    """`contracts` with source values labelled `source` and missing IV / greeks computed (see module doc).
    `asof`: when the prices were current (the session's close for an after-hours or weekend snapshot); `spot`:
    the underlying price at that time. Values already labelled (borrow_source_iv) keep their label."""
    asof_day = asof.astimezone(NY).date()
    out = []
    for c in contracts:
        iv, iv_src = (c.iv, c.iv_source or source) if c.iv else (None, None)
        greeks_src = (c.greeks_source or source) if c.delta is not None else None
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
