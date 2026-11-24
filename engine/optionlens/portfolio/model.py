"""What a broker reports about the user's accounts, normalized (pure): holdings, account balances, FX rates.

Account numbers never leave this layer unmasked: a Holding / Account carries `account_id` (an opaque id, an HMAC of
the number under a key that lives only in this process, so it cannot be turned back into the number) and `account`
(the masked label, e.g. "Margin •••145"). The full number exists only inside the broker adapter that needs it for its
own calls (Questrade's accounts/:id/...), and is never logged or returned."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Broker = Literal["questrade", "ibkr"]
Kind = Literal["stock", "option", "other"]
BROKERS: tuple[Broker, ...] = ("questrade", "ibkr")
BROKER_LABEL = {"questrade": "Questrade", "ibkr": "IBKR"}
SHOWN_DIGITS = 3
_ID_KEY = secrets.token_bytes(32)   # per process: ids are stable while the engine runs, meaningless outside it


def mask_account(number: str) -> str:
    """'•••' + the last SHOWN_DIGITS characters (all of a shorter number is hidden)."""
    n = str(number).strip()
    return "•••" + (n[-SHOWN_DIGITS:] if len(n) > SHOWN_DIGITS else "")


def account_id(broker: str, number: str) -> str:
    """An opaque, process-stable id of an account (not reversible to its number)."""
    return hmac.new(_ID_KEY, f"{broker}:{str(number).strip()}".encode(), hashlib.sha256).hexdigest()[:12]


def account_label(number: str, kind: str | None = None) -> str:
    """e.g. "Margin •••145" (kind as the broker names it, when known)."""
    return f"{kind} {mask_account(number)}" if kind else mask_account(number)


@dataclass(frozen=True, slots=True)
class Holding:
    """One position as a broker reports it. `qty`: shares, or contracts (+ long / - short); `avg_cost` and the prices:
    per share (an option's premium per share: × multiplier for one contract)."""
    broker: Broker
    account_id: str
    account: str                    # masked label
    kind: Kind
    symbol: str                     # the instrument: a ticker, or the option's OSI code (else the broker's symbol)
    underlying: str                 # the stock / index the position is on
    currency: str
    qty: float
    avg_cost: float | None
    multiplier: float = 1.0
    osi: str | None = None
    expiry: date | None = None
    right: Literal["C", "P"] | None = None
    strike: float | None = None
    broker_price: float | None = None     # the broker's current price, when it reports one
    broker_day_pnl: float | None = None   # the broker's own day P/L, when it reports one
    prev_close: float | None = None       # previous close per share, when the broker reports one


@dataclass(frozen=True, slots=True)
class Balance:
    """One currency side of an account (or its base currency): net liquidation value, cash, buying power."""
    currency: str
    net_liquidation: float | None
    cash: float | None
    buying_power: float | None


@dataclass(frozen=True, slots=True)
class Account:
    broker: Broker
    account_id: str
    account: str                    # masked label
    balances: tuple[Balance, ...] = ()


@dataclass(frozen=True, slots=True)
class FxRate:
    """`quote` per 1 `base` (e.g. base USD, quote CAD, rate 1.38: 1 USD = 1.38 CAD), as a broker implied it."""
    base: str
    quote: str
    rate: float
    source: str                     # e.g. "Questrade balances (combined CAD ÷ combined USD)"
    asof: datetime


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    broker: Broker
    fetched_at: datetime
    accounts: tuple[Account, ...] = ()
    holdings: tuple[Holding, ...] = ()
    fx: tuple[FxRate, ...] = field(default_factory=tuple)
