"""Market sessions for live quotes (America/New_York, NYSE trading days): "regular" 09:30 to the (possibly early)
close, "extended" 04:00-09:30 and close-20:00 (US stocks; options do not trade then), else "closed"."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

from ..analytics.calendar import NY, OPEN, close_time, is_trading_day

Session = Literal["regular", "extended", "closed"]
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)


def session_at(ts: datetime) -> Session:
    t = ts.astimezone(NY)
    d, now = t.date(), t.time()
    if not is_trading_day(d) or not EXTENDED_OPEN <= now < EXTENDED_CLOSE:
        return "closed"
    return "regular" if OPEN <= now < close_time(d) else "extended"


def next_live_start(ts: datetime) -> datetime:
    """When live quotes next start (the next trading day's 04:00 ET extended session) after a closed moment `ts`."""
    t = ts.astimezone(NY)
    d: date = t.date()
    for _ in range(15):
        start = datetime.combine(d, EXTENDED_OPEN, NY)
        if is_trading_day(d) and start > t:
            return start
        d += timedelta(days=1)
    return datetime.combine(d, EXTENDED_OPEN, NY)
