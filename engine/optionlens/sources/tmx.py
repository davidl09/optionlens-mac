"""TMX Money (money.tmx.com) earnings of Canadian listings. UNOFFICIAL: the GraphQL endpoint behind the TMX Money
site (https://app-money.tmx.com/graphql), not a documented API, so it may change or close without notice; a failure
makes the source unavailable for the run (sources/ca_earnings.py asks the next one).

Operations (as the site's own pages send them, read 2026-11-26; tests/fixtures/tmx_*.json are recorded answers):
- getEnhancedEarningsForDate(date): the TSX / TSX Venture earnings calendar of one day (the site's Earnings Calendar):
  symbol, announceTime (BTO = before the open, AMC = after the close, N/A / DMT = not specified), estimatedEps,
  actualEps, epsSurprisePercent (numbers, or "N/A"). History reaches back years; dates ahead carry estimates. A day
  without reporters is answered with an error ("Cannot read properties of undefined (reading 'map')") and null data.
- getWSHEventData(symbols): Wall Street Horizon corporate events of a list of tickers (the quote page's "Upcoming
  earnings"): event_type EAD = earnings announcement date, event_name "Q4 2026 Earnings Announcement-Before Mkt"
  (also "-After Mkt", "-During Mkt"), event_status CON (confirmed) / UNC (unconfirmed) / HIS (past), event_date
  MM/DD/YYYY, qmSymbol "TECK.B:CA" naming the listing (stock_symbol may be another market's ticker: DML -> DNN).
  Unknown tickers: data {"error": "No matching companies found"}.

Tickers: TMX names a listing of either exchange by its bare Toronto ticker (RY, TECK.B, AP.UN), i.e.
markets.ticker(symbol). Parsers are pure (a `to_symbol` callback maps a TMX ticker to the canonical symbol, None to
skip it); fetchers do I/O via net, identifying as OptionLens.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable, Sequence

from .. import net
from ..analytics import markets
from ..models import EarningsEvent, Timing

NAME = "tmx"
URL = "https://app-money.tmx.com/graphql"
HEADERS = {"User-Agent": net.HONEST_USER_AGENT, "Content-Type": "application/json"}
UPCOMING_BATCH = 50          # tickers per getWSHEventData request (the site asks for watchlists of this size)
EMPTY_DAY = "Cannot read properties of undefined"
_CALENDAR_TIMING: dict[str, Timing] = {"BTO": "bmo", "AMC": "amc"}
_WSH_TIMING: dict[str, Timing] = {"before mkt": "bmo", "after mkt": "amc"}   # "during mkt": unknown (no BMO/AMC)
_QUARTER = re.compile(r"^(Q[1-4] \d{4})\b")

EARNINGS_DAY_QUERY = """query getEnhancedEarningsForDate($date: String!) {
  getEnhancedEarningsForDate(date: $date) {
    symbol companyName announceTime estimatedEps actualEps epsSurprisePercent epsSurpriseDollar
  }
}"""
UPCOMING_QUERY = """query WSH($symbols: [String]) {
  quote: getWSHEventData(symbols: $symbols) { data }
}"""

ToSymbol = Callable[[str], "str | None"]


class Unexpected(ValueError):
    """TMX answered, but not in the shape these parsers know (the endpoint changed, or an error)."""


def ticker(symbol: str) -> str:
    """TMX's ticker of a Canadian listing: RCI.B.TO -> RCI.B, XYZ.VN -> XYZ."""
    if not markets.is_canadian(symbol):
        raise ValueError(f"{symbol} is not a Canadian listing")
    return markets.ticker(symbol)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ticker_of(v: Any) -> str | None:
    s = str(v).strip().upper() if isinstance(v, str) else ""
    return s or None


def _data(payload: Any, op: str) -> Any:
    """`data[op]` of a GraphQL answer; [] for TMX's empty-day error; Unexpected for any other error or shape."""
    if not isinstance(payload, dict):
        raise Unexpected(f"{op}: not a JSON object")
    errors = payload.get("errors") or []
    value = (payload.get("data") or {}).get(op)
    if errors:
        msgs = [str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors]
        if value is None and all(EMPTY_DAY in m for m in msgs):
            return []
        raise Unexpected(f"{op}: {'; '.join(msgs)[:200]}")
    return value


def parse_earnings_day(payload: Any, on: date, to_symbol: ToSymbol) -> list[EarningsEvent]:
    """getEnhancedEarningsForDate -> events of `on` (surprise as a fraction; tickers `to_symbol` maps to None are
    left out)."""
    rows = _data(payload, "getEnhancedEarningsForDate")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise Unexpected("getEnhancedEarningsForDate: not a list")
    out = []
    for r in rows:
        t = _ticker_of(r.get("symbol")) if isinstance(r, dict) else None
        sym = to_symbol(t) if t else None
        if sym is None:
            continue
        sp = _num(r.get("epsSurprisePercent"))
        out.append(EarningsEvent(sym, on, _CALENDAR_TIMING.get(str(r.get("announceTime")), "unknown"), None,
                                 _num(r.get("actualEps")), _num(r.get("estimatedEps")),
                                 None if sp is None else sp / 100.0, None, NAME))
    return out


def _wsh_timing(name: str) -> Timing:
    tail = name.rsplit("-", 1)[-1].strip().lower() if "-" in name else ""
    return _WSH_TIMING.get(tail, "unknown")


def parse_upcoming(payload: Any, to_symbol: ToSymbol, today: date) -> dict[str, EarningsEvent]:
    """getWSHEventData -> {symbol: its next earnings announcement (EAD, not past, on/after today)} with the time of
    day from the event name and the fiscal quarter ("Q4 2026")."""
    events = (_data(payload, "quote") or {}).get("data")
    if isinstance(events, dict):          # {"error": "No matching companies found"}
        return {}
    if not isinstance(events, list):
        raise Unexpected("getWSHEventData: no event list")
    out: dict[str, EarningsEvent] = {}
    for e in events:
        if not isinstance(e, dict) or e.get("event_type") != "EAD" or e.get("event_status") == "HIS":
            continue
        qm = str(e.get("qmSymbol") or "")
        if not qm.endswith(":CA") or (sym := to_symbol(qm[:-3].upper())) is None:
            continue
        try:
            d = datetime.strptime(str(e.get("event_date")), "%m/%d/%Y").date()
        except ValueError:
            continue
        if d < today or (sym in out and out[sym].date <= d):
            continue
        name = str(e.get("event_name") or "")
        q = _QUARTER.match(name)
        out[sym] = EarningsEvent(sym, d, _wsh_timing(name), q.group(1) if q else None, None, None, None, None, NAME)
    return out


# ----------------------------------------------------------------------------- fetchers
async def _query(op: str, query: str, variables: dict) -> Any:
    return await net.post_json(URL, {"operationName": op, "variables": variables, "query": query}, headers=HEADERS)


async def fetch_earnings_day(on: date, to_symbol: ToSymbol) -> list[EarningsEvent]:
    """The TSX / TSX Venture earnings calendar of one day (FetchError when TMX fails or answers unexpectedly)."""
    payload = await _query("getEnhancedEarningsForDate", EARNINGS_DAY_QUERY, {"date": on.isoformat()})
    try:
        return parse_earnings_day(payload, on, to_symbol)
    except Unexpected as e:
        raise net.FetchError(URL, 200, f"TMX {e}") from e


async def fetch_upcoming(symbols: Sequence[str], today: date) -> tuple[dict[str, EarningsEvent], str | None]:
    """Next earnings of Canadian `symbols` (UPCOMING_BATCH tickers per request, one request after another):
    ({symbol: event}, None), or what was answered before a failure and its reason."""
    by_ticker = {ticker(s): s for s in symbols}
    tickers = list(by_ticker)
    out: dict[str, EarningsEvent] = {}
    for i in range(0, len(tickers), UPCOMING_BATCH):
        try:
            payload = await _query("WSH", UPCOMING_QUERY, {"symbols": tickers[i:i + UPCOMING_BATCH]})
            out.update(parse_upcoming(payload, by_ticker.get, today))
        except (net.FetchError, Unexpected) as e:
            return out, str(e)
    return out, None
