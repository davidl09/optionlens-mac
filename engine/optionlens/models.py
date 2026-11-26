"""Frozen dataclasses shared across layers (SPEC §3). No behaviour beyond trivial derivations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Right = Literal["C", "P"]
Timing = Literal["bmo", "amc", "unknown"]


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    ts: datetime
    price: float | None
    change: float | None
    change_pct: float | None  # fraction
    open: float | None
    high: float | None
    low: float | None
    prev_close: float | None
    volume: int | None
    bid: float | None
    ask: float | None
    iv30: float | None  # fraction
    delayed: bool | None = None  # pushed from delayed (not real-time) market data; None = unknown
    vwap: float | None = None    # the session's volume-weighted average price as the source reported it; None = not sent
    halted: bool | None = None   # trading in the symbol is halted (Questrade isHalted); None = not reported


@dataclass(frozen=True, slots=True)
class OptionContract:
    osi: str
    symbol: str
    expiry: date
    strike: float
    right: Right
    bid: float | None
    ask: float | None
    last: float | None
    change: float | None
    volume: int | None  # None = unknown (never 0)
    oi: int | None      # None = unknown (never 0)
    iv: float | None  # None when the feed reports 0
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None
    theo: float | None
    last_ts: datetime | None = None   # when `last` traded (None = unknown)
    iv_source: str | None = None      # chain source name, 'computed_mid' / 'computed_last', None = no IV
    greeks_source: str | None = None  # chain source name, 'computed', None = no greeks

    @property
    def mid(self) -> float | None:
        """(bid+ask)/2 when both > 0, else last."""
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    quote: Quote
    contracts: tuple[OptionContract, ...]
    fetched_at: datetime
    delayed: bool | None = None  # see Quote.delayed
    # When the contract list was listed, if it was a saved one (IBKR's contract server unavailable); None = listed now.
    contracts_listed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None


@dataclass(frozen=True, slots=True)
class IntradayBar:
    """One intraday OHLCV bar; `ts` is its start (aware)."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None
    vwap: float | None = None   # the bar's own VWAP when the source sent one (Questrade candles); None = not sent


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    symbol: str
    date: date
    timing: Timing
    fiscal_quarter: str | None
    eps_actual: float | None
    eps_estimate: float | None
    surprise_pct: float | None  # fraction
    num_estimates: int | None
    source: str | None = None   # 'nasdaq' (US listings) | 'tmx' | 'yahoo' (Canadian listings, sources/ca_earnings.py)


@dataclass(frozen=True, slots=True)
class Dividend:
    symbol: str
    ex_date: date
    amount: float
    pay_date: date | None
    record_date: date | None
    declared_date: date | None
    kind: str
    source: str | None = None    # 'nasdaq' | 'ibkr-adjusted' | 'questrade' (analytics.dividends.SOURCE_PRIORITY)
    discrepancy: bool = False    # merged rows: the sources' amounts for this ex-date differ by > DISCREPANCY_TOLERANCE


@dataclass(frozen=True, slots=True)
class Fundamentals:
    """One day's reference figures of a symbol as its source reported them (Questrade GET v1/symbols/:id)."""
    symbol: str
    date: date
    source: str
    pe: float | None
    eps: float | None
    market_cap: float | None
    shares_outstanding: float | None
    high_52w: float | None
    low_52w: float | None
    avg_volume_20d: float | None
    avg_volume_3m: float | None
    sector: str | None
    industry_group: str | None
    industry_subgroup: str | None
    dividend_yield: float | None   # fraction (Questrade sends percent)
    prev_close: float | None       # the price the source's pe / marketCap / yield refer to


@dataclass(frozen=True, slots=True)
class ExtendedQuote:
    """A Level 1 quote collected in extended hours: the last trade (any session) vs the last regular-hours trade."""
    symbol: str
    ts: datetime                    # when it was fetched
    price: float | None             # last trade, extended hours included (Questrade lastTradePrice)
    regular_price: float | None     # last regular-hours trade (Questrade lastTradePriceTrHrs)
    volume: int | None              # the day's volume as reported
    last_trade_at: datetime | None
    halted: bool | None
    delayed: bool | None


def normalize_symbol(raw: str) -> str:
    """Canonical symbol: upper-case, no '^'/'_' index prefix, class shares with '.' (BRK/B -> BRK.B)."""
    return raw.strip().upper().lstrip("^_").replace("/", ".")
