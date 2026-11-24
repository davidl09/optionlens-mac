"""Exchange trading calendars: NYSE (US listings and OPRA options) and TSX (Toronto / TSX Venture listings and
Montréal Exchange options). Regular holidays and 13:00 early closes; pure functions, no I/O. Unscheduled closures
(e.g. national days of mourning) are not modelled.

Both exchanges trade 09:30-16:00 Eastern (Toronto and New York share a time zone), so every time here is
America/New_York. The module-level functions (is_trading_day, close_time, ...) are NYSE's, as before; per-symbol
code asks markets.calendar_for(symbol) for the symbol's own TradingCalendar.

TSX rules (tsx.com "Calendars & Trading Hours", 2026 and 2027 lists, read 2026-11-24): closed New Year's Day,
Family Day (3rd Monday of February), Good Friday, Victoria Day (the Monday before May 25), Canada Day, Civic
Holiday (1st Monday of August), Labour Day, Thanksgiving (2nd Monday of October), Christmas Day and Boxing Day; a
holiday on a weekend is taken on the next weekday that is not itself a holiday ("In Lieu of Christmas Day - Monday,
December 27, 2027", "In Lieu of Boxing Day - Tuesday, December 28, 2027"); Christmas Eve on a weekday closes at
13:00 ("Closing at 1:00 PM (TSX/TSXV)"; no other early close is listed, Dec 31 is a full day). US-only holidays
(MLK, Memorial Day, Juneteenth, Independence Day, US Thanksgiving) are TSX trading days.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Callable
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
    """NYSE: Saturday holidays move to Friday, Sunday to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


# ----------------------------------------------------------------------------- NYSE
def _nyse_holidays(year: int) -> frozenset[date]:
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


def _nyse_early_closes(year: int, holidays: frozenset[date]) -> frozenset[date]:
    ec: set[date] = set()
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in holidays:
        ec.add(jul3)
    ec.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # day after Thanksgiving
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in holidays:
        ec.add(dec24)
    return frozenset(ec)


# ----------------------------------------------------------------------------- TSX
def _victoria_day(year: int) -> date:
    """The Monday before May 25 (May 18-24)."""
    d = date(year, 5, 24)
    return d - timedelta(days=d.weekday())


def _tsx_holidays(year: int) -> frozenset[date]:
    """TSX closures (module doc): weekend holidays move to the next free weekday, in date order, so a weekend
    Christmas and Boxing Day become Monday and Tuesday."""
    fixed = [date(year, 1, 1), _nth_weekday(year, 2, 0, 3), _easter(year) - timedelta(days=2), _victoria_day(year),
             date(year, 7, 1), _nth_weekday(year, 8, 0, 1), _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2),
             date(year, 12, 25), date(year, 12, 26)]
    taken: set[date] = set()
    for d in sorted(fixed):
        while d.weekday() >= 5 or d in taken:
            d += timedelta(days=1)
        taken.add(d)
    return frozenset(h for h in taken if h.year == year)


def _tsx_early_closes(year: int, holidays: frozenset[date]) -> frozenset[date]:
    dec24 = date(year, 12, 24)
    return frozenset({dec24}) if dec24.weekday() < 5 and dec24 not in holidays else frozenset()


# ----------------------------------------------------------------------------- calendar
class TradingCalendar:
    """One exchange's sessions: trading days, holidays, early closes and session arithmetic (aware NY times)."""

    def __init__(self, name: str, holiday_rule: Callable[[int], frozenset[date]],
                 early_rule: Callable[[int, frozenset[date]], frozenset[date]]):
        self.name = name
        self.holidays = lru_cache(maxsize=None)(holiday_rule)
        self.early_closes = lru_cache(maxsize=None)(lambda year: early_rule(year, self.holidays(year)))

    def __repr__(self) -> str:
        return f"TradingCalendar({self.name})"

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays(d.year)

    def is_early_close(self, d: date) -> bool:
        return self.is_trading_day(d) and d in self.early_closes(d.year)

    def close_time(self, d: date) -> time:
        return EARLY_CLOSE if self.is_early_close(d) else REGULAR_CLOSE

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def prev_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def add_trading_days(self, d: date, n: int) -> date:
        """Move n trading days from d (n may be negative). n=0 returns d rolled forward if it is not a trading day."""
        if n == 0:
            return d if self.is_trading_day(d) else self.next_trading_day(d)
        step = self.next_trading_day if n > 0 else self.prev_trading_day
        for _ in range(abs(n)):
            d = step(d)
        return d

    def trading_days(self, start: date, end: date) -> list[date]:
        """Inclusive list of trading days in [start, end]."""
        out, d = [], start
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def is_market_open(self, ts: datetime | None = None) -> bool:
        ts = (ts or now_ny()).astimezone(NY)
        d = ts.date()
        return self.is_trading_day(d) and OPEN <= ts.time() < self.close_time(d)

    def last_completed_session(self, ts: datetime | None = None) -> date:
        """Most recent trading day whose session has closed (as of ts)."""
        ts = (ts or now_ny()).astimezone(NY)
        d = ts.date()
        if self.is_trading_day(d) and ts.time() >= self.close_time(d):
            return d
        return self.prev_trading_day(d)

    def is_monthly_expiry(self, d: date) -> bool:
        """Standard monthly expiry: third Friday, or the trading day before when that Friday is a holiday (OPRA and
        the Montréal Exchange alike, e.g. Good Friday)."""
        tf = third_friday(d.year, d.month)
        if self.is_trading_day(tf):
            return d == tf
        return d == self.prev_trading_day(tf)


NYSE = TradingCalendar("NYSE", _nyse_holidays, _nyse_early_closes)
TSX = TradingCalendar("TSX", _tsx_holidays, _tsx_early_closes)
CALENDARS = (NYSE, TSX)


def any_trading_day(d: date) -> bool:
    """Whether any exchange OptionLens follows trades on d (schedules of jobs that serve every market)."""
    return any(c.is_trading_day(d) for c in CALENDARS)


def latest_close_time(d: date) -> time:
    """The latest close among the exchanges trading on d (REGULAR_CLOSE when none does)."""
    return max((c.close_time(d) for c in CALENDARS if c.is_trading_day(d)), default=REGULAR_CLOSE)


# NYSE, module-level (the US default every caller used before calendars were per exchange)
holidays = NYSE.holidays
early_closes = NYSE.early_closes
is_trading_day = NYSE.is_trading_day
is_early_close = NYSE.is_early_close
close_time = NYSE.close_time
next_trading_day = NYSE.next_trading_day
prev_trading_day = NYSE.prev_trading_day
add_trading_days = NYSE.add_trading_days
trading_days = NYSE.trading_days
is_market_open = NYSE.is_market_open
last_completed_session = NYSE.last_completed_session
is_monthly_expiry = NYSE.is_monthly_expiry


def now_ny() -> datetime:
    return datetime.now(NY)


def year_fraction_to_expiry(expiry: date, ts: datetime | None = None) -> float:
    """Calendar-time year fraction until 16:00 ET on expiry (min 1 minute; OPRA and Montréal Exchange options both
    stop trading at 16:00 ET on expiry)."""
    ts = (ts or now_ny()).astimezone(NY)
    exp_dt = datetime.combine(expiry, REGULAR_CLOSE, NY)
    seconds = max((exp_dt - ts).total_seconds(), 60.0)
    return seconds / (365.0 * 86400.0)


def third_friday(year: int, month: int) -> date:
    return _nth_weekday(year, month, 4, 3)
