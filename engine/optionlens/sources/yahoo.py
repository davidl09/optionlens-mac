"""Yahoo Finance next earnings date of a Canadian listing (the fallback after TMX Money). UNOFFICIAL: Yahoo publishes
no API; these are the endpoints its own pages call, so they may change or close without notice, and a failure makes
the source unavailable for the run (sources/ca_earnings.py).

Endpoints:
- a cookie from https://fc.yahoo.com (answered with HTTP 404 while setting it), then a "crumb" from
  https://query2.finance.yahoo.com/v1/test/getcrumb for that cookie (the handshake Yahoo's quote API requires);
- https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=calendarEvents&crumb=…:
  quoteSummary.result[0].calendarEvents.earnings = {earningsDate: [{raw: epoch s, fmt}, …] (one date, or the two
  ends of an estimated window), isEarningsDateEstimate, earningsAverage: {raw}, …}. Unknown symbol: HTTP 404.
Yahoo's past-quarter modules (earningsHistory) give fiscal quarter ends, not report dates, so they are not used.

OptionLens asks as itself (net.HONEST_USER_AGENT), once a second, and gives up at the first 429: Yahoo throttles
clients that do not look like a browser, and OptionLens does not pretend to be one, so Yahoo may simply be
unavailable (checked 2026-11-26: the crumb endpoint answered "429 Too Many Requests" to an honest client; the
quoteSummary shape here follows Yahoo's published answers, tests/fixtures/yahoo_quote_summary_RY.TO.json).

Symbols: Yahoo writes a Canadian listing with "-" for the class dot and ".V" for the TSX Venture
(RCI.B.TO -> RCI-B.TO, AP.UN.TO -> AP-UN.TO, XYZ.VN -> XYZ.V).
"""
from __future__ import annotations

import weakref
from datetime import date, datetime, time, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import httpx

from .. import net
from ..analytics import markets
from ..analytics.calendar import OPEN, REGULAR_CLOSE
from ..models import EarningsEvent, Timing

NAME = "yahoo"
COOKIE_URL = "https://fc.yahoo.com"
CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
HEADERS = {"User-Agent": net.HONEST_USER_AGENT}
TORONTO = ZoneInfo("America/Toronto")
_EXCHANGE_SUFFIX = {"TSX": ".TO", "TSXV": ".V"}


def yahoo_symbol(symbol: str) -> str:
    """Yahoo's symbol of a Canadian listing: RCI.B.TO -> RCI-B.TO, XYZ.VN -> XYZ.V."""
    exchange = markets.listing_exchange(symbol)
    if exchange is None:
        raise ValueError(f"{symbol} is not a Canadian listing")
    return markets.ticker(symbol).replace(".", "-") + _EXCHANGE_SUFFIX[exchange]


def _raw(v: Any) -> float | None:
    x = v.get("raw") if isinstance(v, dict) else v
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _day_and_timing(epoch: float) -> tuple[date, Timing]:
    """An earningsDate timestamp -> (date, timing). Midnight UTC is a date without a time; otherwise the Toronto time
    says before the open / after the close (a time in between says nothing)."""
    utc = datetime.fromtimestamp(epoch, timezone.utc)
    if utc.time() == time(0, 0):
        return utc.date(), "unknown"
    local = utc.astimezone(TORONTO)
    t = local.time()
    return local.date(), ("bmo" if t < OPEN else "amc" if t >= REGULAR_CLOSE else "unknown")


def parse_upcoming(payload: Any, symbol: str, today: date) -> EarningsEvent | None:
    """quoteSummary(calendarEvents) -> the next earnings event of `symbol` on/after today, or None. An estimated
    window (two different days) keeps its first day with an unknown time."""
    res = ((payload or {}).get("quoteSummary") or {}).get("result") if isinstance(payload, dict) else None
    earn = ((res[0] if isinstance(res, list) and res else {}).get("calendarEvents") or {}).get("earnings") or {}
    stamps = sorted(x for v in earn.get("earningsDate") or [] if (x := _raw(v)) is not None)
    days = [_day_and_timing(x) for x in stamps]
    future = [(d, t) for d, t in days if d >= today]
    if not future:
        return None
    d, timing = future[0]
    window = len({x for x, _ in days}) > 1
    if window or earn.get("isEarningsDateEstimate") is True:
        timing = "unknown"
    return EarningsEvent(symbol, d, timing, None, None, _raw(earn.get("earningsAverage")), None, None, NAME)


# ----------------------------------------------------------------------------- fetchers
# The crumb belongs to the cookie, and the cookie to the HTTP client of the running event loop (net.client()).
_crumbs: "weakref.WeakKeyDictionary[httpx.AsyncClient, str]" = weakref.WeakKeyDictionary()


async def _crumb(renew: bool = False) -> str:
    client = net.client()
    if not renew and (c := _crumbs.get(client)):
        return c
    try:
        await net.get(COOKIE_URL, headers=HEADERS, max_throttles=0)
    except net.FetchError as e:
        if e.status != 404:          # fc.yahoo.com sets the cookie on a 404 page
            raise
    r = await net.get(CRUMB_URL, headers=HEADERS, max_throttles=0)
    crumb = r.text.strip()
    if not crumb or len(crumb) > 64 or any(ch.isspace() for ch in crumb):
        raise net.FetchError(CRUMB_URL, r.status_code, "no crumb in the answer")
    _crumbs[client] = crumb
    return crumb


async def _summary(symbol: str) -> Any:
    url = SUMMARY_URL.format(symbol=yahoo_symbol(symbol))
    for renew in (False, True):
        params = {"modules": "calendarEvents", "crumb": await _crumb(renew)}
        try:
            return await net.get_json(url, params, headers=HEADERS, max_throttles=0)
        except net.FetchError as e:
            if e.status != 401 or renew:     # 401 "Invalid Crumb": renew the crumb once
                raise
    raise AssertionError("unreachable")


async def fetch_upcoming(symbols: Sequence[str], today: date) -> tuple[dict[str, EarningsEvent], str | None]:
    """Next earnings of Canadian `symbols`, one request after another: ({symbol: event}, None), or what was answered
    before Yahoo failed and why. A symbol Yahoo does not know (404) has no answer."""
    out: dict[str, EarningsEvent] = {}
    for s in symbols:
        try:
            ev = parse_upcoming(await _summary(s), s, today)
        except net.FetchError as e:
            if e.status == 404:
                continue
            return out, str(e)
        if ev is not None:
            out[s] = ev
    return out, None
