"""Questrade account JSON -> portfolio holdings (pure). Read-only account calls (questrade.com/api/documentation,
"Account calls"; the pages may refuse automated readers, so the shapes below are the documented field tables and
samples as client libraries reproduce them, and parsing is tolerant):

- GET v1/accounts -> {"accounts": [{"type": "Margin", "number": "26598145", "status": "Active", "isPrimary": true,
  "isBilling": true, "clientAccountType": "Individual"}], "userId": 3000124};
- GET v1/accounts/:id/positions -> {"positions": [{"symbol": "THI.TO", "symbolId": 38738, "openQuantity": 100,
  "closedQuantity": 0, "currentMarketValue": 6017, "currentPrice": 60.17, "averageEntryPrice": 60.23,
  "closedPnl": 0, "openPnl": -6, "totalCost": false, "isRealTime": "Individual", "isUnderReorg": false}]} (the
  sample's odd totalCost / isRealTime values are not read); the field table spells "closedPnL" / "openPnL" and some
  answers add "dayPnl": every spelling is read;
- GET v1/accounts/:id/balances -> {"perCurrencyBalances": [{"currency": "CAD", "cash": 243971.7, "marketValue": 6017,
  "totalEquity": 249988.7, "buyingPower": 496367.2, "maintenanceExcess": 248183.6, "isRealTime": false}, {USD ...}],
  "combinedBalances": [same fields, everything converted into CAD / into USD], "sodPerCurrencyBalances": [...],
  "sodCombinedBalances": [...]};
- GET v1/symbols?ids=a,b (a market-data call) -> {"symbols": [{"symbol", "symbolId", "currency", "prevDayClosePrice",
  "securityType" ("Stock", "Option", ...), "optionType" ("Call" / "Put"), "optionRoot", "optionExpiryDate",
  "optionStrikePrice", "optionContractDeliverables": {"underlyings": [{"multiplier": 100, "underlyingSymbol": "AAPL",
  "underlyingSymbolId": 8049}], "cashInLieu": 0}}]}.

Positions without open quantity (closed today) are dropped; securities other than stocks (ETFs are "Stock") and
options (bonds, mutual funds, rights, ...) are kept as kind "other" (no greeks, valued at Questrade's price).
Prices: `averageEntryPrice` / `currentPrice` per share (an option's per share of premium). The FX rate is implied by the combined balances: combined CAD total equity ÷
combined USD total equity (the conversion Questrade itself applied)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from ...models import normalize_symbol
from ...portfolio.model import Account, Balance, FxRate, Holding, account_id, account_label
from ...analytics.markets import is_canadian
from ..osi import osi_code, osi_root

BROKER = "questrade"
OPTION_MULTIPLIER = 100.0
FX_SOURCE = "Questrade balances (combined CAD ÷ combined USD)"


def _num(v: Any) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _get(d: Mapping, *keys: str) -> Any:
    return next((d[k] for k in keys if k in d), None)


@dataclass(frozen=True, slots=True)
class QAccount:
    number: str
    type: str | None
    status: str | None


def parse_accounts(payload: Any) -> list[QAccount]:
    """The open accounts (status other than Closed), in Questrade's order."""
    rows = payload.get("accounts") if isinstance(payload, Mapping) else None
    out = []
    for r in rows or []:
        if not isinstance(r, Mapping) or not str(r.get("number") or "").strip():
            continue
        if str(r.get("status") or "").lower() == "closed":
            continue
        out.append(QAccount(str(r["number"]).strip(), r.get("type") or None, r.get("status") or None))
    return out


@dataclass(frozen=True, slots=True)
class QPosition:
    symbol: str
    symbol_id: int | None
    qty: float
    avg_price: float | None
    current_price: float | None
    day_pnl: float | None


def parse_positions(payload: Any) -> list[QPosition]:
    rows = payload.get("positions") if isinstance(payload, Mapping) else None
    out = []
    for r in rows or []:
        if not isinstance(r, Mapping):
            continue
        qty = _num(r.get("openQuantity"))
        if not qty:
            continue
        sid = _num(r.get("symbolId"))
        out.append(QPosition(str(r.get("symbol") or ""), int(sid) if sid is not None else None, qty,
                             _num(r.get("averageEntryPrice")), _num(r.get("currentPrice")),
                             _num(_get(r, "dayPnl", "dayPnL"))))
    return out


def _balance(r: Mapping) -> Balance | None:
    cur = str(r.get("currency") or "").upper()
    if not cur:
        return None
    return Balance(cur, _num(r.get("totalEquity")), _num(r.get("cash")), _num(r.get("buyingPower")))


def parse_balances(payload: Any) -> tuple[tuple[Balance, ...], tuple[Balance, ...]]:
    """(per-currency balances, combined balances)."""
    def rows(key: str) -> tuple[Balance, ...]:
        raw = payload.get(key) if isinstance(payload, Mapping) else None
        return tuple(b for r in raw or [] if isinstance(r, Mapping) and (b := _balance(r)) is not None)
    return rows("perCurrencyBalances"), rows("combinedBalances")


def implied_usdcad(combined: Iterable[Balance], asof: datetime) -> FxRate | None:
    """CAD per USD from one account's combined balances (both sides > 0), else None."""
    by = {b.currency: b.net_liquidation for b in combined}
    cad, usd = by.get("CAD"), by.get("USD")
    if not cad or not usd or cad <= 0 or usd <= 0:
        return None
    return FxRate("USD", "CAD", cad / usd, FX_SOURCE, asof)


@dataclass(frozen=True, slots=True)
class QSymbol:
    symbol: str
    currency: str | None
    prev_close: float | None
    is_option: bool
    security_type: str | None = None
    root: str | None = None
    underlying: str | None = None
    expiry: date | None = None
    right: str | None = None
    strike: float | None = None
    multiplier: float | None = None


def _date(v: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(v)).date() if v else None
    except ValueError:
        return None


def parse_symbols(payload: Any) -> dict[int, QSymbol]:
    """symbolId -> what positions need of a symbol (currency, previous close, option terms)."""
    rows = payload.get("symbols") if isinstance(payload, Mapping) else None
    out: dict[int, QSymbol] = {}
    for r in rows or []:
        if not isinstance(r, Mapping) or _num(r.get("symbolId")) is None:
            continue
        sid = int(r["symbolId"])
        cur = str(r.get("currency") or "").upper() or None
        prev = _num(r.get("prevDayClosePrice"))
        kind = str(r.get("optionType") or "").lower()
        sec = str(r.get("securityType") or "") or None
        expiry, strike = _date(r.get("optionExpiryDate")), _num(r.get("optionStrikePrice"))
        if kind not in ("call", "put") or expiry is None or strike is None:
            out[sid] = QSymbol(str(r.get("symbol") or ""), cur, prev, False, sec)
            continue
        unds = (r.get("optionContractDeliverables") or {}).get("underlyings") or []
        first = unds[0] if unds and isinstance(unds[0], Mapping) else {}
        root = str(r.get("optionRoot") or "").upper() or None
        underlying = str(first.get("underlyingSymbol") or "") or root
        out[sid] = QSymbol(str(r.get("symbol") or ""), cur, prev, True, sec, root,
                           normalize_symbol(underlying) if underlying else None, expiry,
                           "C" if kind == "call" else "P", strike, _num(first.get("multiplier")))
    return out


def _currency_of(symbol: str) -> str:
    """Without symbol details: Canadian listings carry an exchange suffix (.TO, .V, .VN, .CN, .NE), else USD."""
    return "CAD" if symbol.upper().rsplit(".", 1)[-1] in ("TO", "V", "VN", "CN", "NE") and "." in symbol else "USD"


def holdings(acct: QAccount, positions: Iterable[QPosition], symbols: Mapping[int, QSymbol]) -> list[Holding]:
    """The account's open positions as Holdings (options with an OSI code from root / expiry / right / strike)."""
    aid, label = account_id(BROKER, acct.number), account_label(acct.number, acct.type)
    out = []
    for p in positions:
        info = symbols.get(p.symbol_id) if p.symbol_id is not None else None
        currency = (info.currency if info and info.currency else None) or _currency_of(p.symbol)
        if info and info.is_option:
            und = info.underlying or normalize_symbol(p.symbol)
            # a Montréal Exchange contract is coded under its Canadian underlying (sources/osi.py), never OPRA's root
            root = osi_root(und) if is_canadian(und) else info.root or und.replace(".", "")
            osi = osi_code(root, info.expiry, info.right, info.strike)  # type: ignore[arg-type]
            out.append(Holding(BROKER, aid, label, "option", osi, und, currency, p.qty, p.avg_price,
                               info.multiplier or OPTION_MULTIPLIER, osi, info.expiry, info.right,  # type: ignore[arg-type]
                               info.strike, p.current_price, p.day_pnl, info.prev_close))
            continue
        sym = normalize_symbol(p.symbol)
        kind = "stock" if info is None or info.security_type in (None, "Stock") else "other"
        out.append(Holding(BROKER, aid, label, kind, sym, sym, currency, p.qty, p.avg_price, 1.0,
                           broker_price=p.current_price, broker_day_pnl=p.day_pnl,
                           prev_close=info.prev_close if info else None))
    return out


def account(acct: QAccount, per_currency: tuple[Balance, ...]) -> Account:
    return Account(BROKER, account_id(BROKER, acct.number), account_label(acct.number, acct.type), per_currency)
