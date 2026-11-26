"""The IBKR bridge's positions push (POST /api/ingest/positions, docs/INTEGRATION.md §6) -> a BrokerSnapshot (pure).

The bridge sends what TWS / IB Gateway reports through its read-only account requests: `reqPositions` (account,
contract, position, average cost) and `reqAccountSummary` (NetLiquidation, TotalCashValue, BuyingPower in the
account's base currency; `$LEDGER:ALL` ExchangeRate per currency). IBKR's `avgCost` is per share for stocks but per
contract (× multiplier) for options: it is divided by the multiplier here, so every average cost is per share.
`ExchangeRate` of currency C is the base-currency value of 1 C: FxRate(base=C, quote=base currency).

IBKR names a contract by ticker, currency and exchange (RY / CAD / TSE), OptionLens by its canonical symbol
(RY.TO, analytics/markets.py): `canonical` maps one to the other, so a Canadian position is valued with the Canadian
listing's data and a Montréal Exchange option gets the code every chain source uses (sources/osi.py). A position
that maps to no listing OptionLens follows keeps IBKR's ticker and its own currency (shown, never valued with
another listing's price)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Mapping

from ..analytics import markets
from ..models import normalize_symbol
from ..sources.osi import osi_code, osi_root
from .model import Account, Balance, BrokerSnapshot, FxRate, Holding, account_id, account_label

BROKER = "ibkr"
FX_SOURCE = "IBKR account summary (ExchangeRate)"


# IBKR's exchange codes of the Canadian listing venues -> the exchange names of analytics.markets.
CA_VENUES = {"TSE": "TSX", "VENTURE": "TSXV"}
Known = Callable[[str], bool]


def canonical(ticker: str, currency: str, exchange: str | None, known: Known) -> str:
    """OptionLens' symbol of the listing an IBKR contract names. IBKR writes US share classes with a space (BRK B ->
    BRK.B). A CAD contract is the Toronto listing of its venue (TSE -> .TO, VENTURE -> .VN); when IBKR names no
    listing venue (an option's exchange is the Montréal Exchange), the listing OptionLens knows (`known`), the TSX
    first. Anything else is IBKR's ticker unchanged."""
    sym = normalize_symbol(ticker).replace(" ", ".")
    if currency != markets.CA.currency:
        return sym
    venue = CA_VENUES.get((exchange or "").upper())
    candidates = [markets.canadian_symbol(sym, v) for v in ([venue] if venue else CA_VENUES.values())]
    return next((c for c in candidates if known(c)), candidates[0] if venue else sym)


def _holding(p: Mapping[str, Any], known: Known) -> Holding | None:
    qty = float(p["qty"])
    if not qty:
        return None
    number = str(p["account"])
    aid, label = account_id(BROKER, number), account_label(number)
    cur = str(p.get("currency") or "USD").upper()
    is_option = p["sec_type"] == "OPT" and p.get("expiry") and p.get("strike") and p.get("right") in ("C", "P")
    # an option's own exchange is where the option trades, not where its underlying is listed
    sym = canonical(str(p["symbol"]), cur, None if is_option else p.get("exchange"), known)
    avg = p.get("avg_cost")
    if is_option:
        mult = float(p.get("multiplier") or 100)
        expiry: date = p["expiry"]
        # a Montréal Exchange contract is coded under its Canadian underlying (sources/osi.py), never OPRA's root
        root = osi_root(sym) if markets.is_canadian(sym) else str(p.get("trading_class") or sym.replace(".", "")).upper()
        osi = osi_code(root, expiry, p["right"], float(p["strike"]))
        return Holding(BROKER, aid, label, "option", osi, sym, cur, qty, float(avg) / mult if avg is not None else None,
                       mult, osi, expiry, p["right"], float(p["strike"]))
    kind = "stock" if p["sec_type"] == "STK" else "other"
    mult = float(p.get("multiplier") or 1) if kind == "other" else 1.0
    return Holding(BROKER, aid, label, kind, sym, sym, cur, qty, float(avg) / mult if avg is not None else None, mult)


def snapshot(body: Mapping[str, Any], received: datetime, known: Known = lambda _symbol: False) -> BrokerSnapshot:
    """`body`: the validated push (dates parsed). `fetched_at` is the bridge's, else `received`. `known`: whether
    OptionLens knows a symbol (`canonical`)."""
    at = body.get("fetched_at") or received
    holdings = tuple(h for p in body.get("positions", ()) if (h := _holding(p, known)) is not None)
    accounts = tuple(
        Account(BROKER, account_id(BROKER, str(a["account"])), account_label(str(a["account"])),
                (Balance(str(a.get("currency") or "USD").upper(), a.get("net_liquidation"), a.get("cash"),
                         a.get("buying_power")),))
        for a in body.get("accounts", ()))
    fx = tuple(FxRate(str(x["currency"]).upper(), str(x["base"]).upper(), float(x["rate"]), FX_SOURCE, at)
               for x in body.get("fx", ()) if x.get("rate") and str(x["currency"]).upper() != str(x["base"]).upper())
    return BrokerSnapshot(BROKER, at, accounts, holdings, fx)
