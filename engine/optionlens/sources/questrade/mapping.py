"""Questrade JSON -> models (pure). Field names as documented at questrade.com/api/documentation (REST
operations, market calls); where a page's field table and its sample JSON disagree, both spellings are read:
- GET v1/symbols/search: results under "symbols" (table) / "symbol" (sample);
- GET v1/symbols/:id/options: expiries under "optionChain" / "options" (sample) / "symbols" (table), each root's
  name under "optionRoot" (table) / "root" (sample);
- POST v1/markets/quotes/options: quotes under "optionQuotes" (sample) / "quotes" (table).

GET v1/symbols/:id (reference data, docs/INTEGRATION.md §8): `dividend` is the amount of ONE payment, not the annual
rate — the sample's AAPL has dividend 0.47 and yield 1.84 at prevDayClosePrice 102.5: 4 × 0.47 / 102.5 = 1.83%, so
`yield` (percent) is the annualized current dividend over the price; `exDate` / `dividendDate` are its ex-date and pay
date. The sub-group is spelled `industrySubGroup` (sample). Equity quotes: `VWAP` is read when Questrade sends it (it
is not in the documented sample; null otherwise), `isHalted` as is, and lastTradePrice (any session) vs
lastTradePriceTrHrs (regular hours) gives the extended-hours move.

Values: prices <= 0 are "no price" (None); a 0 bid counts only against a real ask; volume and open interest are
kept as reported (None when absent, never invented); `volatility` is in percent (sample: 52.374257) and becomes
a fraction; without a volatility the greeks (model outputs of it) are None. `delay` (> 0 / true) marks delayed
data. Contracts of non-standard roots (adjusted deliverables) or multipliers other than 100 are dropped; for
SPX / NDX / RUT an expiry listed under both the AM and the PM root keeps the PM root (osi.format_osi).
"""
from __future__ import annotations

import math

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from ...analytics import markets
from ...analytics.calendar import NY
from ...models import (Bar, ChainSnapshot, Dividend, ExtendedQuote, Fundamentals, IntradayBar, OptionContract, Quote,
                       Right, normalize_symbol)
from ..osi import PM_ROOTS, format_osi, standard_roots

STANDARD_MULTIPLIER = 100


def first_of(d: Mapping, *keys: str) -> Any:
    """The value of the first of `keys` present in `d` (the docs spell some fields two ways)."""
    for k in keys:
        if k in d:
            return d[k]
    raise KeyError(f"none of {keys} in Questrade response")


def _price(v: Any) -> float | None:
    return float(v) if v is not None and float(v) > 0 else None


def _count(v: Any) -> int | None:
    return None if v is None else int(v)


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def _day(v: Any) -> date | None:
    """A documented date-time ("2014-08-07T00:00:00.000000-04:00") -> its calendar date."""
    t = _ts(v)
    return t.date() if t else None


def _text(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _flag(v: Any) -> bool | None:
    return None if v is None else bool(v)


def is_delayed(item: Mapping) -> bool:
    """`delay`: minutes (option quotes) or a boolean (equity quotes); 0 / false = real-time."""
    return bool(item.get("delay"))


# ----------------------------------------------------------------------------- symbols
def _is_listing_of(r: Mapping, market: markets.Market) -> bool:
    """A search result is a listing of `market`: its currency (when given), and for Canada a TSX / TSX Venture
    listing (Questrade writes Canadian symbols with that suffix, e.g. RY.TO, BMO.PRJ.TO)."""
    if r.get("currency") not in (None, market.currency):
        return False
    return market is not markets.CA or r.get("listingExchange") in (None, *markets.CA_SUFFIXES.values())


@dataclass(frozen=True)
class Listing:
    """One symbols/search result: what resolution chose and the log says."""
    symbol_id: int
    symbol: str                       # as Questrade writes it, e.g. SPX.IN, RY.TO
    security_type: str | None         # "Stock" | "Index" | ... (ETFs are "Stock")
    description: str | None
    currency: str | None
    listing_exchange: str | None

    def describe(self) -> str:
        return (f"{self.symbol} (symbolId {self.symbol_id}, {self.security_type or '?'}, "
                f"{self.description or 'no description'}, {self.currency or '?'} {self.listing_exchange or '?'})")


def listings(payload: Mapping, symbol: str) -> list[Listing]:
    """The listings of `symbol` (canonical, e.g. BRK.B, RY.TO, SPX) in a symbols/search answer, best first: listings
    of exactly that symbol in its market (USD for US symbols; CAD on the TSX / TSX Venture for Canadian ones, so RY and
    RY.TO never resolve to each other); an index result (securityType "Index") matches by the part before any '.'
    suffix (SPX.IN). Listings of the type that fits the symbol come first — an index for the cash indices
    (markets.is_index), another type for everything else — then Questrade's order."""
    market = markets.market_of(symbol)
    out = []
    for r in first_of(payload, "symbols", "symbol") or []:
        if not _is_listing_of(r, market) or r.get("symbolId") is None:
            continue
        name = normalize_symbol(str(r.get("symbol", "")))
        base = name.split(".")[0] if r.get("securityType") == "Index" else name
        if base == symbol:
            out.append(Listing(int(r["symbolId"]), str(r.get("symbol")), _text(r.get("securityType")),
                               _text(r.get("description")), _text(r.get("currency")), _text(r.get("listingExchange"))))
    index = markets.is_index(symbol)
    return sorted(out, key=lambda x: (x.security_type == "Index") != index)


def pick_symbol_id(payload: Mapping, symbol: str) -> int | None:
    """The symbolId of the best listing of `symbol` in a symbols/search answer (listings), None when Questrade has
    none."""
    found = listings(payload, symbol)
    return found[0].symbol_id if found else None


@dataclass(frozen=True)
class SymbolInfo:
    """GET v1/symbols/:id of one symbol: what the chain needs plus the reference data (module doc)."""
    has_options: bool
    prev_close: float | None
    pe: float | None = None
    eps: float | None = None
    market_cap: float | None = None        # in the listing's currency
    shares_outstanding: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    avg_volume_20d: float | None = None
    avg_volume_3m: float | None = None
    sector: str | None = None              # industrySector, e.g. "BasicMaterials"
    industry_group: str | None = None
    industry_subgroup: str | None = None
    dividend: float | None = None          # one payment, $/share
    dividend_yield: float | None = None    # fraction (Questrade: percent)
    ex_date: date | None = None
    pay_date: date | None = None
    name: str | None = None                # description
    security_type: str | None = None       # "Stock" | "Option" | "Index" | ... (ETFs are "Stock")
    listing_exchange: str | None = None    # "TSX" | "TSXV" | "NYSE" | ...
    currency: str | None = None


def symbol_info(payload: Mapping) -> SymbolInfo:
    """GET v1/symbols/:id -> SymbolInfo of its one symbol (exact sample field names; 0 / absent = unknown, except
    eps and pe which may be negative / zero as reported)."""
    s = payload["symbols"][0]
    y = s.get("yield")
    return SymbolInfo(
        bool(s.get("hasOptions")), _price(s.get("prevDayClosePrice")),
        pe=_num(s.get("pe")), eps=_num(s.get("eps")), market_cap=_price(s.get("marketCap")),
        shares_outstanding=_price(s.get("outstandingShares")), high_52w=_price(s.get("highPrice52")),
        low_52w=_price(s.get("lowPrice52")), avg_volume_20d=_price(s.get("averageVol20Days")),
        avg_volume_3m=_price(s.get("averageVol3Months")), sector=_text(s.get("industrySector")),
        industry_group=_text(s.get("industryGroup")), industry_subgroup=_text(s.get("industrySubGroup")),
        dividend=_price(s.get("dividend")), dividend_yield=None if y is None else float(y) / 100.0,
        ex_date=_day(s.get("exDate")), pay_date=_day(s.get("dividendDate")), name=_text(s.get("description")),
        security_type=_text(s.get("securityType")), listing_exchange=_text(s.get("listingExchange")),
        currency=_text(s.get("currency")))


def fundamentals(info: SymbolInfo, symbol: str, day: date) -> Fundamentals:
    """The day's reference snapshot of `symbol` from its SymbolInfo (source 'questrade')."""
    return Fundamentals(symbol, day, "questrade", info.pe, info.eps, info.market_cap, info.shares_outstanding,
                        info.high_52w, info.low_52w, info.avg_volume_20d, info.avg_volume_3m, info.sector,
                        info.industry_group, info.industry_subgroup, info.dividend_yield, info.prev_close)


def current_dividend(info: SymbolInfo, symbol: str) -> Dividend | None:
    """The current dividend (one payment) with its ex-date and pay date (source 'questrade'); None without an
    amount or an ex-date."""
    if info.dividend is None or info.ex_date is None:
        return None
    return Dividend(symbol, info.ex_date, info.dividend, info.pay_date, None, None, "cash", source="questrade")


# ----------------------------------------------------------------------------- chain structure
@dataclass(frozen=True)
class Listed:
    """One listed contract of the chain structure."""
    symbol_id: int
    expiry: date
    strike: float
    right: Right


def listed_contracts(payload: Mapping, symbol: str) -> tuple[dict[date, str], dict[int, Listed]]:
    """GET v1/symbols/:id/options -> ({expiry: expiryDate as Questrade wrote it}, {option symbolId: Listed}) of the
    standard contracts (see module doc)."""
    roots = standard_roots(symbol)
    pm = PM_ROOTS.get(symbol)
    expiries: dict[date, str] = {}
    per_root: dict[tuple[date, str], list[Listed]] = {}
    for e in first_of(payload, "optionChain", "options", "symbols") or []:
        raw = e["expiryDate"]
        day = datetime.fromisoformat(raw).date()
        for chain in e.get("chainPerRoot") or []:
            root = str(first_of(chain, "optionRoot", "root")).replace(".", "")
            if root not in roots or chain.get("multiplier", STANDARD_MULTIPLIER) != STANDARD_MULTIPLIER:
                continue
            expiries.setdefault(day, raw)
            rows = per_root.setdefault((day, root), [])
            for k in chain.get("chainPerStrikePrice") or []:
                strike = float(k["strikePrice"])
                for right, key in (("C", "callSymbolId"), ("P", "putSymbolId")):
                    if k.get(key) is not None:
                        rows.append(Listed(int(k[key]), day, strike, right))  # type: ignore[arg-type]
    out: dict[int, Listed] = {}
    for (day, root), rows in per_root.items():
        if pm and root != pm and (day, pm) in per_root:
            continue    # AM-settled duplicate of a PM expiry: one OSI per (expiry, strike, right)
        out.update({c.symbol_id: c for c in rows})
    return expiries, out


def pick_expiries(expiries: Mapping[date, str], today: date, n: int) -> list[date]:
    """The first `n` expiries on/after `today`."""
    return sorted(d for d in expiries if d >= today)[:n]


def option_filters(underlying_id: int, expiries: Iterable[str], lo: float, hi: float) -> list[dict]:
    """POST v1/markets/quotes/options filters: one per expiry (calls and puts), strikes in [lo, hi].

    Bounds are widened to whole cents: Questrade answers a strike with float noise (37.964999999999996) with
    400 code 1013 ("Requesting anything other than 'application/json'"), verified live on 2026-11-21."""
    lo_c, hi_c = math.floor(lo * 100) / 100, math.ceil(hi * 100) / 100
    return [{"underlyingId": underlying_id, "expiryDate": raw, "minstrikePrice": lo_c, "maxstrikePrice": hi_c}
            for raw in expiries]


# ----------------------------------------------------------------------------- quotes
def parse_quote(q: Mapping, symbol: str, prev_close: float | None, fetched_at: datetime, final: bool) -> Quote:
    """An equity / index Level 1 quote (GET v1/markets/quotes?ids=) -> Quote. A closing snapshot (`final`) takes
    lastTradePriceTrHrs, the last regular-hours trade (after 16:00 lastTradePrice can be an extended-hours one)."""
    bid, ask = _price(q.get("bidPrice")), _price(q.get("askPrice"))
    order = ("lastTradePriceTrHrs", "lastTradePrice") if final else ("lastTradePrice", "lastTradePriceTrHrs")
    price = next((p for k in order if (p := _price(q.get(k))) is not None), None)
    if price is None and bid and ask:
        price = (bid + ask) / 2.0
    change = price - prev_close if price is not None and prev_close else None
    return Quote(symbol=symbol, ts=fetched_at, price=price, change=change,
                 change_pct=change / prev_close if change is not None else None,
                 open=_price(q.get("openPrice")), high=_price(q.get("highPrice")), low=_price(q.get("lowPrice")),
                 prev_close=prev_close, volume=_count(q.get("volume")), bid=bid, ask=ask, iv30=None,
                 delayed=is_delayed(q), vwap=_price(q.get("VWAP")), halted=_flag(q.get("isHalted")))


def extended_quote(q: Mapping, symbol: str, fetched_at: datetime) -> ExtendedQuote:
    """An equity Level 1 quote collected in extended hours: lastTradePrice (any session) and lastTradePriceTrHrs (the
    last regular-hours trade), the day's volume, the last trade's time, isHalted, delay."""
    return ExtendedQuote(symbol, fetched_at, _price(q.get("lastTradePrice")), _price(q.get("lastTradePriceTrHrs")),
                         _count(q.get("volume")), _ts(q.get("lastTradeTime")), _flag(q.get("isHalted")),
                         is_delayed(q))


def option_quotes(payload: Mapping) -> dict[int, Mapping]:
    """POST v1/markets/quotes/options answer -> {symbolId: quote}."""
    return {int(q["symbolId"]): q for q in first_of(payload, "optionQuotes", "quotes") or []}


def contract(symbol: str, c: Listed, q: Mapping | None) -> OptionContract:
    """A listed contract with its quote (None: listed but not quoted -> all market fields None)."""
    q = q or {}
    ask = _price(q.get("askPrice"))
    bid_raw = q.get("bidPrice")
    bid = float(bid_raw) if bid_raw is not None and float(bid_raw) >= 0 and (ask is not None or float(bid_raw) > 0) \
        else None
    vol = q.get("volatility")
    iv = float(vol) / 100.0 if vol is not None and float(vol) > 0 else None
    greek = (lambda k: _num(q.get(k))) if iv is not None else (lambda k: None)
    return OptionContract(
        osi=format_osi(symbol, c.expiry, c.right, c.strike), symbol=symbol, expiry=c.expiry, strike=c.strike,
        right=c.right, bid=bid, ask=ask, last=_price(q.get("lastTradePrice")), change=None,
        volume=_count(q.get("volume")), oi=_count(q.get("openInterest")), iv=iv,
        delta=greek("delta"), gamma=greek("gamma"), theta=greek("theta"), vega=greek("vega"), rho=greek("rho"),
        theo=None, last_ts=_ts(q.get("lastTradeTime")) if q.get("lastTradePrice") else None)


def snapshot(quote: Quote, listed: Iterable[Listed], quotes: Mapping[int, Mapping], fetched_at: datetime) -> ChainSnapshot:
    """The chain of the `listed` contracts (every one, unquoted ones with null fields); delayed when the underlying
    quote or any option quote is."""
    rows = sorted(listed, key=lambda c: (c.expiry, c.strike, c.right))
    delayed = bool(quote.delayed) or any(is_delayed(q) for q in quotes.values())
    return ChainSnapshot(quote, tuple(contract(quote.symbol, c, quotes.get(c.symbol_id)) for c in rows),
                         fetched_at, delayed)


# ----------------------------------------------------------------------------- live quotes (live/questrade.py)
QUOTE_LIVE_FIELDS = ("price", "change", "change_pct", "bid", "ask", "open", "high", "low", "prev_close", "volume",
                     "vwap", "halted")
OPTION_LIVE_FIELDS = ("bid", "ask", "mid", "last", "volume", "oi", "iv", "delta", "gamma", "theta", "vega")


def live_quote(q: Mapping, symbol: str, prev_close: float | None, at: datetime) -> dict:
    """An equity / index Level 1 quote -> the live patch of `symbol` ({QUOTE_LIVE_FIELDS..., delayed}); the same
    reading as parse_quote (a live price is the last trade, else the mid)."""
    p = parse_quote(q, symbol, prev_close, at, final=False)
    return {**{k: getattr(p, k) for k in QUOTE_LIVE_FIELDS}, "delayed": bool(p.delayed)}


def live_option(symbol: str, c: Listed, q: Mapping) -> dict:
    """An option Level 1 quote -> the live patch of its contract ({OPTION_LIVE_FIELDS..., delayed}); the same
    reading as contract()."""
    o = contract(symbol, c, q)
    mid = (o.bid + o.ask) / 2.0 if o.bid is not None and o.ask is not None else None
    return {"bid": o.bid, "ask": o.ask, "mid": mid, "last": o.last, "volume": o.volume, "oi": o.oi, "iv": o.iv,
            "delta": o.delta, "gamma": o.gamma, "theta": o.theta, "vega": o.vega, "delayed": is_delayed(q)}


# ----------------------------------------------------------------------------- candles
# GET v1/markets/candles/:id `interval` values (Enumerations, "Historical Data Granularity").
CANDLE_INTERVAL = {"1m": "OneMinute", "5m": "FiveMinutes", "15m": "FifteenMinutes", "1h": "OneHour"}
DAILY_INTERVAL = "OneDay"


def candle_time(ts: datetime) -> str:
    """A candles startTime / endTime: New York time with its offset, as in the docs' sample (2014-10-01T00:00:00-05:00)."""
    return ts.astimezone(NY).isoformat(timespec="seconds")


def candles(payload: Mapping) -> list[IntradayBar]:
    """`candles` -> IntradayBar (start = the candle's `start`, `VWAP` when the candle has one); candles without a
    positive OHLC are dropped."""
    out = []
    for c in payload.get("candles") or []:
        ts = _ts(c.get("start"))
        o, h, l, cl = (_price(c.get(k)) for k in ("open", "high", "low", "close"))
        if ts is None or ts.tzinfo is None or None in (o, h, l, cl):
            continue
        out.append(IntradayBar(ts, o, h, l, cl, _count(c.get("volume")), _price(c.get("VWAP"))))
    return out


def daily_bars(payload: Mapping, symbol: str) -> list[Bar]:
    """OneDay `candles` -> daily Bars (the session = the candle's start date in New York), ascending."""
    out = {c.ts.astimezone(NY).date(): c for c in candles(payload)}
    return [Bar(symbol, d, c.open, c.high, c.low, c.close, c.volume) for d, c in sorted(out.items())]
