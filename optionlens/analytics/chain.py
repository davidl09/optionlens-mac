"""ChainArrays: one symbol's option chain as column arrays (the shared input of vol, chain metrics
and strategy screeners).

Rows are sorted by (expiry, strike, right) with calls before puts at equal strike. Missing numbers
are NaN. ``mid`` follows SPEC §3: (bid+ask)/2 when both > 0, else last (NaN if neither).
``t`` is the BSM year fraction to 16:00 ET on expiry (calendar.year_fraction_to_expiry); ``dte``
is calendar days from the as-of date (New York) to expiry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import numpy as np

from .calendar import NY, year_fraction_to_expiry


@dataclass(frozen=True, eq=False)
class ChainArrays:
    symbol: str
    spot: float
    asof: datetime
    r: float
    q: float
    hv30: float | None
    iv_rank: float | None
    next_earnings: date | None
    expiries: tuple[date, ...]      # unique, ascending
    exp_idx: np.ndarray             # int index into expiries, per row
    strike: np.ndarray
    is_call: np.ndarray             # bool
    bid: np.ndarray
    ask: np.ndarray
    last: np.ndarray
    mid: np.ndarray
    volume: np.ndarray              # float (0 when missing)
    oi: np.ndarray                  # float (0 when missing)
    iv: np.ndarray                  # NaN when unknown
    delta: np.ndarray
    gamma: np.ndarray
    theta: np.ndarray
    vega: np.ndarray
    t: np.ndarray                   # years to expiry
    dte: np.ndarray                 # int calendar days
    osi: np.ndarray                 # object: OSI codes (None when not supplied)
    src: np.ndarray                 # object: the source contract rows (None when not supplied)
    next_reaction: date | None = None   # expected reaction session of the next earnings (timing-aware)

    def __len__(self) -> int:
        return len(self.strike)

    @property
    def asof_date(self) -> date:
        return self.asof.astimezone(NY).date() if self.asof.tzinfo else self.asof.date()

    @property
    def exp_t(self) -> np.ndarray:
        """Year fraction per unique expiry."""
        out = np.full(len(self.expiries), np.nan)
        out[self.exp_idx] = self.t
        return out

    @property
    def exp_dte(self) -> np.ndarray:
        out = np.zeros(len(self.expiries), dtype=int)
        out[self.exp_idx] = self.dte
        return out

    def earnings_before_expiry(self) -> np.ndarray:
        """bool per unique expiry: the next earnings REACTION session falls between the as-of date
        and that expiry (an amc report on expiry day reacts after it, so it does not count). Falls
        back to the event date when no reaction day is known."""
        ev = self.next_reaction or self.next_earnings
        return np.array([ev is not None and self.asof_date <= ev <= e for e in self.expiries], dtype=bool)

    def expiry_slices(self) -> list[slice]:
        """Contiguous row slice per unique expiry (rows are expiry-sorted)."""
        bounds = np.searchsorted(self.exp_idx, np.arange(len(self.expiries) + 1))
        return [slice(int(bounds[i]), int(bounds[i + 1])) for i in range(len(self.expiries))]

    @classmethod
    def from_columns(cls, *, expiry: Sequence[date], strike, right, bid, ask, last, volume, oi, iv,
                     delta, gamma, theta, vega, spot: float, asof: datetime, symbol: str = "",
                     osi: Sequence[str] | None = None, src: Sequence[Any] | None = None,
                     r: float = 0.0, q: float = 0.0, hv30: float | None = None,
                     iv_rank: float | None = None, next_earnings: date | None = None,
                     next_reaction: date | None = None) -> "ChainArrays":
        """Build from column sequences (e.g. a DB result). ``right`` holds "C"/"P"; None entries
        become NaN (volume/oi: 0); iv <= 0 means unknown. ``src`` (optional) are the source rows used
        by OptionRow outputs (chainmetrics.top_contracts / oi_changes)."""
        exp_list = list(expiry)
        expiries = tuple(sorted(set(exp_list)))
        pos = {e: i for i, e in enumerate(expiries)}
        exp_idx = np.array([pos[e] for e in exp_list], dtype=int)

        def col(x):
            a = np.asarray(x, dtype=object) if not isinstance(x, np.ndarray) or x.dtype == object else x
            if a.dtype == object:
                return np.array([np.nan if v is None else float(v) for v in a], dtype=float)
            return a.astype(float)

        k = col(strike)
        is_call = np.asarray([str(v).upper() == "C" for v in right], dtype=bool)
        order = np.lexsort((~is_call, k, exp_idx))
        asof_day = asof.astimezone(NY).date() if asof.tzinfo else asof.date()
        exp_t = np.array([year_fraction_to_expiry(e, asof) for e in expiries])
        exp_dte = np.array([(e - asof_day).days for e in expiries], dtype=int)
        bid_a, ask_a, last_a = col(bid)[order], col(ask)[order], col(last)[order]
        both = (bid_a > 0) & (ask_a > 0)
        iv_a = col(iv)[order]
        n = len(k)
        src_a = np.empty(n, dtype=object)
        if src is not None:
            src_a[:] = list(src)
        osi_a = np.empty(n, dtype=object)
        if osi is not None:
            osi_a[:] = list(osi)
        ei = exp_idx[order]
        return cls(
            symbol=symbol, spot=float(spot), asof=asof, r=float(r), q=float(q), hv30=hv30,
            iv_rank=iv_rank, next_earnings=next_earnings, expiries=expiries, exp_idx=ei,
            strike=k[order], is_call=is_call[order], bid=bid_a, ask=ask_a, last=last_a,
            mid=np.where(both, (bid_a + ask_a) / 2.0, last_a),
            volume=np.nan_to_num(col(volume)[order]), oi=np.nan_to_num(col(oi)[order]),
            iv=np.where(iv_a > 0, iv_a, np.nan),
            delta=col(delta)[order], gamma=col(gamma)[order], theta=col(theta)[order], vega=col(vega)[order],
            t=exp_t[ei] if n else np.zeros(0), dte=exp_dte[ei] if n else np.zeros(0, int),
            osi=osi_a[order], src=src_a[order], next_reaction=next_reaction,
        )

    @classmethod
    def from_contracts(cls, contracts: Iterable[Any], spot: float, asof: datetime, *,
                       symbol: str | None = None, **kw) -> "ChainArrays":
        """Build from OptionContract-like rows (attribute access: osi, expiry, strike, right, bid,
        ask, last, volume, oi, iv, delta, gamma, theta, vega). Keyword args as from_columns."""
        rows = list(contracts)
        cols = {name: [getattr(c, name, None) for c in rows]
                for name in ("expiry", "strike", "right", "bid", "ask", "last", "volume", "oi", "iv",
                             "delta", "gamma", "theta", "vega", "osi")}
        return cls.from_columns(**cols, spot=spot, asof=asof, src=rows,
                                symbol=symbol or (rows[0].symbol if rows else ""), **kw)

    @classmethod
    def from_snapshot(cls, chain, now: datetime | None = None, **kw) -> "ChainArrays":
        """Build from a ChainSnapshot (spot = quote.price, as-of = now or chain.fetched_at)."""
        return cls.from_contracts(chain.contracts, chain.quote.price, now or chain.fetched_at,
                                  symbol=chain.quote.symbol, **kw)


def option_row(c: Any, spot: float) -> dict:
    """OptionRow (types.ts) from an OptionContract-like row."""
    mid = getattr(c, "mid", None)
    if mid is None and not hasattr(type(c), "mid"):
        b, a = c.bid, c.ask
        mid = (b + a) / 2.0 if b and a and b > 0 and a > 0 else c.last
    itm = (c.strike < spot) if c.right == "C" else (c.strike > spot)
    return {
        "osi": c.osi, "expiry": c.expiry, "strike": float(c.strike), "right": c.right,
        "bid": c.bid, "ask": c.ask, "mid": mid, "last": c.last, "change": c.change,
        "volume": int(c.volume or 0), "oi": int(c.oi or 0), "iv": c.iv,
        "delta": c.delta, "gamma": c.gamma, "theta": c.theta, "vega": c.vega, "rho": c.rho,
        "theo": c.theo, "itm": bool(itm),
    }
