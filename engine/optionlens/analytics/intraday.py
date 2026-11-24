"""Intraday bars: intervals, ranges, sessions and the time windows to fetch (pure, no I/O).

A range is a number of sessions (1d, 5d) or calendar months (1m, 3m) ending now. Sources are always asked for
every hour they have (pre-market 04:00 to after-hours 20:00 ET), so one stored series serves both the regular-hours
view and the extended-hours view; `is_regular` filters on read. Times are aware datetimes (UTC inside, New York for
session rules).
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from .calendar import NY, OPEN, close_time, is_trading_day, prev_trading_day, trading_days

INTERVAL_S: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
# The intervals each range offers (the first is its default): a sensible number of bars on screen.
RANGE_INTERVALS: dict[str, tuple[str, ...]] = {
    "1d": ("1m", "5m", "15m"), "5d": ("5m", "15m", "1h"), "1m": ("15m", "1h"), "3m": ("1h",),
}
RETENTION = {"1m": timedelta(days=10), "5m": timedelta(days=60), "15m": timedelta(days=365), "1h": timedelta(days=365)}
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)
# Bars this close to now are asked for again on the next request (the current bar is still forming, and a delayed
# feed delivers the last minutes late), so a stored span never claims them.
RECENT = timedelta(minutes=20)

Span = tuple[datetime, datetime]


def check(interval: str, rng: str) -> None:
    """ValueError unless `interval` is offered for `rng`."""
    if rng not in RANGE_INTERVALS:
        raise ValueError(f"range must be one of {', '.join(RANGE_INTERVALS)}")
    if interval not in RANGE_INTERVALS[rng]:
        raise ValueError(f"interval {interval} is not offered for range {rng} (use {', '.join(RANGE_INTERVALS[rng])})")


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, NY).astimezone(timezone.utc)


def _sessions_back(today: date, n: int) -> date:
    d = today
    for _ in range(n - 1):
        d = prev_trading_day(d)
    return d


def _months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def window(rng: str, now: datetime, extended: bool) -> Span:
    """[start, end] of `rng` as of `now`: from the first session's pre-market open to now. The latest session is today
    once it has started (04:00 ET in the extended view, 09:30 ET otherwise), else the previous trading day."""
    ny = now.astimezone(NY)
    started = is_trading_day(ny.date()) and ny.time() >= (EXTENDED_OPEN if extended else OPEN)
    last = ny.date() if started else prev_trading_day(ny.date())
    if rng in ("1d", "5d"):
        first = _sessions_back(last, int(rng[0]))
    else:
        first = _months_back(last, int(rng[0]))
    return _at(first, EXTENDED_OPEN), now.astimezone(timezone.utc)


def is_regular(ts: datetime) -> bool:
    """Whether a bar starting at `ts` is in the regular session (09:30 to the close, ET)."""
    ny = ts.astimezone(NY)
    return is_trading_day(ny.date()) and OPEN <= ny.time() < close_time(ny.date())


def missing(covered: Iterable[Span], want: Span) -> list[Span]:
    """Parts of `want` no span of `covered` contains."""
    out: list[Span] = []
    cursor, end = want
    for s, e in sorted(covered):
        if e <= cursor:
            continue
        if s >= end:
            break
        if s > cursor:
            out.append((cursor, s))
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end:
        out.append((cursor, end))
    return out


def session_windows(span: Span) -> list[Span]:
    """The trading days' 04:00-20:00 ET windows inside `span` (nights, weekends and holidays have no bars)."""
    s, e = span
    out = []
    for d in trading_days(s.astimezone(NY).date(), e.astimezone(NY).date()):
        a, b = max(s, _at(d, EXTENDED_OPEN)), min(e, _at(d, EXTENDED_CLOSE))
        if a < b:
            out.append((a, b))
    return out


def request_windows(span: Span, interval: str, max_bars: int) -> list[Span]:
    """Request windows covering the session hours of `span`, each holding at most `max_bars` bars of `interval`
    (consecutive days are grouped; a day longer than the limit is cut)."""
    step = timedelta(seconds=INTERVAL_S[interval])
    out: list[Span] = []
    cur: Span | None = None
    bars = 0
    for a, b in session_windows(span):
        while a < b:
            n = -(-(b - a) // step)
            if cur is not None and bars + n <= max_bars:
                cur, bars = (cur[0], b), bars + n
                break
            if cur is not None:
                out.append(cur)
            if n <= max_bars:
                cur, bars = (a, b), n
                break
            cut = a + step * max_bars
            out.append((a, cut))
            cur, a = None, cut
    if cur is not None:
        out.append(cur)
    return out


def settled(end: datetime, now: datetime) -> datetime:
    """Up to when a fetch that ended at `end` (as of `now`) may count as stored for good (see RECENT)."""
    return min(end, now - RECENT)
