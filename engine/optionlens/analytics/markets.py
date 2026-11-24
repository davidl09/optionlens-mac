"""Markets OptionLens follows: US (NYSE/Nasdaq listings, OPRA options, USD) and Canada (TSX / TSX Venture listings,
Montréal Exchange options, CAD). Pure; the one place that knows how a symbol maps to its market.

Canonical symbols (docs/SPEC.md §3 "Symbols"): a US listing is its bare ticker (RY = Royal Bank's NYSE listing,
BRK.B); a Canadian listing is its Toronto ticker with the exchange suffix Questrade uses: `.TO` for the TSX, `.VN`
for the TSX Venture (RY.TO, RCI.B.TO, AP.UN.TO). So RY and RY.TO are two symbols, never confused. Canadian listings
in the universe are CAD-traded, so a symbol's market fixes its currency (TSX units trading in USD, e.g. `DLR.U.TO`,
are left out).
"""
from __future__ import annotations

from dataclasses import dataclass

from .calendar import NYSE, TSX, TradingCalendar

CA_SUFFIXES = {".TO": "TSX", ".VN": "TSXV"}


@dataclass(frozen=True)
class Market:
    code: str               # "US" | "CA"
    label: str
    currency: str           # ISO 4217
    calendar: TradingCalendar
    options_exchange: str   # where its listed options trade


US = Market("US", "US", "USD", NYSE, "OPRA")
CA = Market("CA", "Canada", "CAD", TSX, "MX")
MARKETS = {m.code: m for m in (US, CA)}


def _suffix(symbol: str) -> str | None:
    s = symbol.upper()
    return next((x for x in CA_SUFFIXES if s.endswith(x) and len(s) > len(x)), None)


def is_canadian(symbol: str) -> bool:
    return _suffix(symbol) is not None


def market_of(symbol: str) -> Market:
    return CA if is_canadian(symbol) else US


def calendar_for(symbol: str) -> TradingCalendar:
    return market_of(symbol).calendar


def currency_of(symbol: str) -> str:
    return market_of(symbol).currency


def listing_exchange(symbol: str) -> str | None:
    """TSX / TSXV for a Canadian symbol (from its suffix); None for a US one (the venue is not tracked)."""
    sfx = _suffix(symbol)
    return CA_SUFFIXES[sfx] if sfx else None


def ticker(symbol: str) -> str:
    """The exchange ticker without the market suffix: RCI.B.TO -> RCI.B; AAPL -> AAPL."""
    sfx = _suffix(symbol)
    return symbol[: -len(sfx)] if sfx else symbol


def canadian_symbol(ticker_: str, exchange: str) -> str:
    """Canonical symbol of a Canadian listing: ticker + the suffix of its exchange (TSX -> .TO, TSXV -> .VN)."""
    sfx = {v: k for k, v in CA_SUFFIXES.items()}[exchange]
    return f"{ticker_.upper()}{sfx}"


def by_market(symbols) -> dict[Market, list[str]]:
    """Symbols grouped by market (input order kept within each group; markets in MARKETS order, empty ones left out)."""
    out: dict[Market, list[str]] = {m: [] for m in MARKETS.values()}
    for s in symbols:
        out[market_of(s)].append(s)
    return {m: syms for m, syms in out.items() if syms}
