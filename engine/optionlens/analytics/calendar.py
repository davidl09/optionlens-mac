"""NYSE trading calendar (regular holidays and 13:00 early closes).

Pure functions; no I/O. Unscheduled closures (e.g. national days of mourning) are not modelled.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
OPEN = time(9, 30)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) weekday (Mon=0) of a month; n=-1 means last."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d: date) -> date | None:
    """Saturday holidays move to Friday, Sunday to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(year: int) -> frozenset[date]:
    hs: set[date] = set()
    # New Year's Day: if on Saturday, NYSE does NOT close the prior Friday (Dec 31).
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        hs.add(ny + timedelta(days=1))
    elif ny.weekday() != 5:
        hs.add(ny)
    hs.add(_nth_weekday(year, 1, 0, 3))   # MLK
    hs.add(_nth_weekday(year, 2, 0, 3))   # Presidents
    hs.add(_easter(year) - timedelta(days=2))  # Good Friday
    hs.add(_nth_weekday(year, 5, 0, -1))  # Memorial
    if year >= 2022:
        hs.add(_observed(date(year, 6, 19)))  # Juneteenth
    hs.add(_observed(date(year, 7, 4)))   # Independence
    hs.add(_nth_weekday(year, 9, 0, 1))   # Labor
    hs.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    hs.add(_observed(date(year, 12, 25)))  # Christmas
    return frozenset(h for h in hs if h.year == year)


@lru_cache(maxsize=None)
def early_closes(year: int) -> frozenset[date]:
    ec: set[date] = set()
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in holidays(year):
        ec.add(jul3)
    ec.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # day after Thanksgiving
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in holidays(year):
        ec.add(dec24)
    return frozenset(ec)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def is_early_close(d: date) -> bool:
    return is_trading_day(d) and d in early_closes(d.year)


def close_time(d: date) -> time:
    return EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def add_trading_days(d: date, n: int) -> date:
    """Move n trading days from d (n may be negative). n=0 returns d rolled forward if d is not a trading day."""
    if n == 0:
        return d if is_trading_day(d) else next_trading_day(d)
    step = next_trading_day if n > 0 else prev_trading_day
    for _ in range(abs(n)):
        d = step(d)
    return d


def trading_days(start: date, end: date) -> list[date]:
    """Inclusive list of trading days in [start, end]."""
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def now_ny() -> datetime:
    return datetime.now(NY)


def is_market_open(ts: datetime | None = None) -> bool:
    ts = (ts or now_ny()).astimezone(NY)
    d = ts.date()
    return is_trading_day(d) and OPEN <= ts.time() < close_time(d)


def last_completed_session(ts: datetime | None = None) -> date:
    """Most recent trading day whose session has closed (as of ts)."""
    ts = (ts or now_ny()).astimezone(NY)
    d = ts.date()
    if is_trading_day(d) and ts.time() >= close_time(d):
        return d
    return prev_trading_day(d)


def year_fraction_to_expiry(expiry: date, ts: datetime | None = None) -> float:
    """Calendar-time year fraction until 16:00 ET on expiry (min 1 minute)."""
    ts = (ts or now_ny()).astimezone(NY)
    exp_dt = datetime.combine(expiry, REGULAR_CLOSE, NY)
    seconds = max((exp_dt - ts).total_seconds(), 60.0)
    return seconds / (365.0 * 86400.0)


def third_friday(year: int, month: int) -> date:
    return _nth_weekday(year, month, 4, 3)


def is_monthly_expiry(d: date) -> bool:
    """Standard monthly expiry: third Friday, or the Thursday before when that Friday is a holiday."""
    tf = third_friday(d.year, d.month)
    if is_trading_day(tf):
        return d == tf
    return d == prev_trading_day(tf)
