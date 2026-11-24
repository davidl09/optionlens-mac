"""MinuteBars: 1-minute OHLCV bars built from the live quotes of the subscribed symbols (pure, no I/O).

The candle source (Questrade) answers the latest minutes late (its candles end ~15 minutes before now, even on a
real-time account), so while a symbol is live its quotes are aggregated here into 1m bars that bridge that lag
(services/intraday.py serves them as source "live" until the source's candles replace them).

- a tick is a trade when the quote's cumulative day volume rose (or, without day volumes, when the price changed):
  a minute without a trade has no bar, as with source candles; the first quote of a symbol only anchors;
- volume: the day-volume delta since the previous quote, counted in the minute of the quote that shows it (a lower day
  volume is a new day: anchored again, not a trade);
- minutes are clock minutes (US time-zone offsets are whole hours: a UTC minute is a New York minute); ticks outside
  the 04:00-20:00 ET sessions only anchor;
- delayed quotes build no bars (their prices are minutes old: stamping them now would misplace them).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..analytics import intraday
from ..models import IntradayBar
from ..analytics.markets import calendar_for

MINUTE_S = 60


@dataclass
class _Bar:
    start: float
    open: float
    high: float
    low: float
    close: float
    volume: int | None

    def bar(self) -> IntradayBar:
        return IntradayBar(datetime.fromtimestamp(self.start, timezone.utc), self.open, self.high, self.low,
                           self.close, self.volume)


@dataclass
class _Symbol:
    price: float | None = None
    volume: int | None = None
    forming: _Bar | None = None
    closed: list[_Bar] = field(default_factory=list)


class MinuteBars:
    def __init__(self) -> None:
        self._syms: dict[str, _Symbol] = {}

    def tick(self, symbol: str, price: float | None, volume: int | None, delayed: bool, at: float) -> None:
        """One live quote of `symbol` at epoch `at` (its last price and cumulative day volume)."""
        if price is None:
            return
        s = self._syms.setdefault(symbol, _Symbol())
        prev_price, prev_volume = s.price, s.volume
        s.price = price
        if volume is not None:
            s.volume = volume
        if prev_price is None or delayed or not intraday.in_sessions(datetime.fromtimestamp(at, timezone.utc),
                                                                     calendar_for(symbol)):
            return
        if volume is not None and prev_volume is not None:
            if volume <= prev_volume:
                return                       # no trade (or a lower day volume: a new day, anchored above)
            delta: int | None = volume - prev_volume
        elif volume is None and price != prev_price:
            delta = None
        else:
            return
        start = at - at % MINUTE_S
        b = s.forming
        if b is not None and b.start != start:
            s.closed.append(b)
            b = s.forming = None
        if b is None:
            s.forming = _Bar(start, price, price, price, price, delta)
            return
        b.high, b.low, b.close = max(b.high, price), min(b.low, price), price
        if delta is not None:
            b.volume = (b.volume or 0) + delta

    def _close_ended(self, s: _Symbol, now: float) -> None:
        if s.forming is not None and now >= s.forming.start + MINUTE_S:
            s.closed.append(s.forming)
            s.forming = None

    def take_closed(self, now: float) -> dict[str, list[IntradayBar]]:
        """The bars whose minute ended (by `now`), per symbol, handed over once (to be stored)."""
        out: dict[str, list[IntradayBar]] = {}
        for sym, s in self._syms.items():
            self._close_ended(s, now)
            if s.closed:
                out[sym] = [b.bar() for b in s.closed]
                s.closed = []
        return out

    def pending(self, symbol: str) -> list[IntradayBar]:
        """The bars of `symbol` not handed over yet: closed ones, then the forming one (ascending)."""
        s = self._syms.get(symbol)
        if s is None:
            return []
        return [b.bar() for b in [*s.closed, *([s.forming] if s.forming else [])]]

    def forget(self, keep: set[str]) -> None:
        """Drop the state of symbols no longer live once their bars were handed over."""
        for sym in [k for k, s in self._syms.items() if k not in keep and not s.closed and s.forming is None]:
            del self._syms[sym]
