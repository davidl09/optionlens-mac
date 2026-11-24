"""Market sessions for live quotes (America/New_York), per market (analytics/markets.py):
- US (NYSE trading days): "regular" 09:30 to the (possibly early) close, "extended" 04:00-09:30 and close-20:00 (US
  stocks; options do not trade then), else "closed";
- Canada (TSX trading days): "regular" 09:30 to the close, else "closed" (the TSX has no pre-market trading and its
  16:15-17:00 extended session trades at the closing price only, so nothing moves outside the regular session)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

from ..analytics.calendar import NY, OPEN
from ..analytics.markets import MARKETS

Session = Literal["regular", "extended", "closed"]
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)
_EXTENDED = {"US": (EXTENDED_OPEN, EXTENDED_CLOSE)}   # markets with extended-hours trading


def session_at(ts: datetime, market: str = "US") -> Session:
    cal = MARKETS[market].calendar
    t = ts.astimezone(NY)
    d, now = t.date(), t.time()
    if not cal.is_trading_day(d):
        return "closed"
    if OPEN <= now < cal.close_time(d):
        return "regular"
    lo, hi = _EXTENDED.get(market, (OPEN, OPEN))
    return "extended" if lo <= now < hi else "closed"


def next_live_start(ts: datetime, market: str = "US") -> datetime:
    """When live quotes of `market` next start (its next trading day's 04:00 ET extended session in the US, 09:30 in
    Canada) after a closed moment `ts`."""
    cal = MARKETS[market].calendar
    first = _EXTENDED.get(market, (OPEN,))[0]
    t = ts.astimezone(NY)
    d: date = t.date()
    for _ in range(15):
        start = datetime.combine(d, first, NY)
        if cal.is_trading_day(d) and start > t:
            return start
        d += timedelta(days=1)
    return datetime.combine(d, first, NY)
