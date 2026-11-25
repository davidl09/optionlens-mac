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

from .calendar import NY, NYSE, OPEN, TradingCalendar

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


def _sessions_back(today: date, n: int, cal: TradingCalendar) -> date:
    d = today
    for _ in range(n - 1):
        d = cal.prev_trading_day(d)
    return d


def _months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


# Sessions follow the symbol's exchange calendar (`cal`, analytics.markets.calendar_for; default NYSE).
def window(rng: str, now: datetime, extended: bool, cal: TradingCalendar = NYSE) -> Span:
    """[start, end] of `rng` as of `now`: from the first session's pre-market open to now. The latest session is today
    once it has started (04:00 ET in the extended view, 09:30 ET otherwise), else the previous trading day."""
    ny = now.astimezone(NY)
    started = cal.is_trading_day(ny.date()) and ny.time() >= (EXTENDED_OPEN if extended else OPEN)
    last = ny.date() if started else cal.prev_trading_day(ny.date())
    if rng in ("1d", "5d"):
        first = _sessions_back(last, int(rng[0]), cal)
    else:
        first = _months_back(last, int(rng[0]))
    return _at(first, EXTENDED_OPEN), now.astimezone(timezone.utc)


def extended_window(day: date, market: str) -> Span:
    """The extended-hours window of trading day `day`: 'pre' 04:00-09:30, 'post' the close-20:00 ET (US listings: the
    TSX has no extended-hours trading)."""
    if market == "pre":
        return _at(day, EXTENDED_OPEN), _at(day, OPEN)
    if market == "post":
        return _at(day, NYSE.close_time(day)), _at(day, EXTENDED_CLOSE)
    raise ValueError(market)


def extended_market(ts: datetime) -> str | None:
    """'pre' | 'post' when `ts` is in a trading day's extended-hours window (extended_window), else None."""
    d = ts.astimezone(NY).date()
    if not NYSE.is_trading_day(d):
        return None
    return next((m for m in ("pre", "post") if (w := extended_window(d, m))[0] <= ts < w[1]), None)


def is_regular(ts: datetime, cal: TradingCalendar = NYSE) -> bool:
    """Whether a bar starting at `ts` is in the regular session (09:30 to the close, ET)."""
    ny = ts.astimezone(NY)
    return cal.is_trading_day(ny.date()) and OPEN <= ny.time() < cal.close_time(ny.date())


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


def session_windows(span: Span, cal: TradingCalendar = NYSE) -> list[Span]:
    """The trading days' 04:00-20:00 ET windows inside `span` (nights, weekends and holidays have no bars)."""
    s, e = span
    out = []
    for d in cal.trading_days(s.astimezone(NY).date(), e.astimezone(NY).date()):
        a, b = max(s, _at(d, EXTENDED_OPEN)), min(e, _at(d, EXTENDED_CLOSE))
        if a < b:
            out.append((a, b))
    return out


def request_windows(span: Span, interval: str, max_bars: int, cal: TradingCalendar = NYSE) -> list[Span]:
    """Request windows covering the session hours of `span`, each holding at most `max_bars` bars of `interval`
    (consecutive days are grouped; a day longer than the limit is cut)."""
    step = timedelta(seconds=INTERVAL_S[interval])
    out: list[Span] = []
    cur: Span | None = None
    bars = 0
    for a, b in session_windows(span, cal):
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


def in_sessions(ts: datetime, cal: TradingCalendar = NYSE) -> bool:
    """Whether `ts` is inside a trading day's 04:00-20:00 ET window (where sources have bars)."""
    ny = ts.astimezone(NY)
    return cal.is_trading_day(ny.date()) and EXTENDED_OPEN <= ny.time() < EXTENDED_CLOSE


def bucket(ts: datetime, interval: str, grid: datetime | None = None) -> datetime:
    """Start of the `interval` bar holding `ts`: on the grid of the bar starting at `grid` within the same New York
    day (a source's bars may start at 09:30), else on the New York clock (1h bars at whole hours)."""
    step = timedelta(seconds=INTERVAL_S[interval])
    if grid is not None and grid <= ts and grid.astimezone(NY).date() == ts.astimezone(NY).date():
        return grid + (ts - grid) // step * step
    ny = ts.astimezone(NY)
    midnight = datetime.combine(ny.date(), time(0), NY)
    return (midnight + (ny.replace(tzinfo=None) - midnight.replace(tzinfo=None)) // step * step).astimezone(timezone.utc)


def aggregate(rows: list[dict], interval: str, grid: datetime | None = None) -> list[dict]:
    """1m bar rows (ascending; ts, open, high, low, close, volume, ...) -> rows of `interval` (see `bucket`)."""
    out: dict[datetime, dict] = {}
    for r in rows:
        t = bucket(r["ts"], interval, grid)
        b = out.get(t)
        if b is None:
            out[t] = {**r, "ts": t}
            continue
        vols = [v for v in (b["volume"], r["volume"]) if v is not None]
        b.update(high=max(b["high"], r["high"]), low=min(b["low"], r["low"]), close=r["close"],
                 volume=sum(vols) if vols else None, vwap=None)   # a merged bar has no VWAP of its own
    return list(out.values())


def with_live(source: list[dict], live_1m: list[dict], interval: str) -> list[dict]:
    """A source's bars of `interval` (ascending) followed by the live-built bars after its newest one: live 1m bars
    aggregated to `interval` on the source's grid. A source's bar always wins: live bars only bridge the minutes the
    source has not answered yet (its candles lag), and never fill a gap inside the source's bars."""
    newest = source[-1]["ts"] if source else None
    live = aggregate(live_1m, interval, newest)
    return source + [r for r in live if newest is None or r["ts"] > newest]


def live_only_from(span: Span, source_ts: Iterable[datetime], live_ts: Iterable[datetime],
                   interval: str) -> datetime | None:
    """Where the part of a fetched `span` that only live bars cover starts (one bar early, on the source's grid), so
    that it is asked for again until the source has its own bars there; None when no live bar follows the source's
    newest bar in `span`."""
    s, e = span
    step = timedelta(seconds=INTERVAL_S[interval])
    newest = max((t for t in source_ts if s <= t <= e), default=None)
    after = newest + step if newest is not None else s
    first = min((t for t in live_ts if after <= t <= e), default=None)
    if first is None:
        return None
    return max(s, bucket(first, interval, newest) - step)


def day_vwap(bars: list[dict], extended: bool = False,
             cal: TradingCalendar = NYSE) -> tuple[list[float | None], str | None]:
    """The session VWAP after each bar (ascending bars {ts, high, low, close, volume, vwap}): per New York trading day,
    Σ(price × volume) / Σ volume over its bars so far, price = the bar's own VWAP when its source sent one, else the
    typical price (high + low + close) / 3. Regular-hours bars only (None for the others) unless `extended`: then
    every bar counts, the day's VWAP starting at its first pre-market bar (as charts with extended hours draw it).
    None before any volume.
    -> (values, how: 'candles' when every bar used had its own VWAP, 'computed' when any used the typical price,
    None without a value)."""
    out: list[float | None] = []
    day = None
    pv = vol = 0.0
    used_own = used_typical = False
    for b in bars:
        if not extended and not is_regular(b["ts"], cal):
            out.append(None)
            continue
        d = b["ts"].astimezone(NY).date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        v = b.get("volume")
        if v:
            own = b.get("vwap")
            price = own if own else (b["high"] + b["low"] + b["close"]) / 3.0
            used_own, used_typical = used_own or bool(own), used_typical or not own
            pv, vol = pv + price * v, vol + v
        out.append(pv / vol if vol > 0 else None)
    how = "computed" if used_typical else ("candles" if used_own else None)
    return out, how
