"""Questrade JSON -> models (pure). Field names as documented at questrade.com/api/documentation (REST
operations, market calls); where a page's field table and its sample JSON disagree, both spellings are read:
- GET v1/symbols/search: results under "symbols" (table) / "symbol" (sample);
- GET v1/symbols/:id/options: expiries under "optionChain" / "options" (sample) / "symbols" (table), each root's
  name under "optionRoot" (table) / "root" (sample);
- POST v1/markets/quotes/options: quotes under "optionQuotes" (sample) / "quotes" (table).

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

from ...models import ChainSnapshot, OptionContract, Quote, Right, normalize_symbol
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


def is_delayed(item: Mapping) -> bool:
    """`delay`: minutes (option quotes) or a boolean (equity quotes); 0 / false = real-time."""
    return bool(item.get("delay"))


# ----------------------------------------------------------------------------- symbols
def pick_symbol_id(payload: Mapping, symbol: str) -> int | None:
    """The symbolId of `symbol` (canonical, e.g. BRK.B) in a symbols/search answer: a USD listing of exactly that
    symbol; for an index (securityType "Index") the part before any '.' suffix. None when Questrade has none."""
    for r in first_of(payload, "symbols", "symbol") or []:
        if r.get("currency") not in (None, "USD"):
            continue
        name = normalize_symbol(str(r.get("symbol", "")))
        base = name.split(".")[0] if r.get("securityType") == "Index" else name
        if base == symbol and r.get("symbolId") is not None:
            return int(r["symbolId"])
    return None


@dataclass(frozen=True)
class SymbolInfo:
    has_options: bool
    prev_close: float | None


def symbol_info(payload: Mapping) -> SymbolInfo:
    """GET v1/symbols/:id -> hasOptions and prevDayClosePrice of its one symbol."""
    s = payload["symbols"][0]
    return SymbolInfo(bool(s.get("hasOptions")), _price(s.get("prevDayClosePrice")))


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
                 delayed=is_delayed(q))


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
