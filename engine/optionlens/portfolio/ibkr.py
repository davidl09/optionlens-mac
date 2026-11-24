"""The IBKR bridge's positions push (POST /api/ingest/positions, docs/INTEGRATION.md §6) -> a BrokerSnapshot (pure).

The bridge sends what TWS / IB Gateway reports through its read-only account requests: `reqPositions` (account,
contract, position, average cost) and `reqAccountSummary` (NetLiquidation, TotalCashValue, BuyingPower in the
account's base currency; `$LEDGER:ALL` ExchangeRate per currency). IBKR's `avgCost` is per share for stocks but per
contract (× multiplier) for options: it is divided by the multiplier here, so every average cost is per share.
`ExchangeRate` of currency C is the base-currency value of 1 C: FxRate(base=C, quote=base currency)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from ..models import normalize_symbol
from ..sources.osi import osi_code
from .model import Account, Balance, BrokerSnapshot, FxRate, Holding, account_id, account_label

BROKER = "ibkr"
FX_SOURCE = "IBKR account summary (ExchangeRate)"


def _holding(p: Mapping[str, Any]) -> Holding | None:
    qty = float(p["qty"])
    if not qty:
        return None
    number = str(p["account"])
    aid, label = account_id(BROKER, number), account_label(number)
    sym = normalize_symbol(str(p["symbol"]))
    cur = str(p.get("currency") or "USD").upper()
    avg = p.get("avg_cost")
    if p["sec_type"] == "OPT" and p.get("expiry") and p.get("strike") and p.get("right") in ("C", "P"):
        mult = float(p.get("multiplier") or 100)
        expiry: date = p["expiry"]
        root = str(p.get("trading_class") or sym.replace(".", "")).upper()
        osi = osi_code(root, expiry, p["right"], float(p["strike"]))
        return Holding(BROKER, aid, label, "option", osi, sym, cur, qty, float(avg) / mult if avg is not None else None,
                       mult, osi, expiry, p["right"], float(p["strike"]))
    kind = "stock" if p["sec_type"] == "STK" else "other"
    mult = float(p.get("multiplier") or 1) if kind == "other" else 1.0
    return Holding(BROKER, aid, label, kind, sym, sym, cur, qty, float(avg) / mult if avg is not None else None, mult)


def snapshot(body: Mapping[str, Any], received: datetime) -> BrokerSnapshot:
    """`body`: the validated push (dates parsed). `fetched_at` is the bridge's, else `received`."""
    at = body.get("fetched_at") or received
    holdings = tuple(h for p in body.get("positions", ()) if (h := _holding(p)) is not None)
    accounts = tuple(
        Account(BROKER, account_id(BROKER, str(a["account"])), account_label(str(a["account"])),
                (Balance(str(a.get("currency") or "USD").upper(), a.get("net_liquidation"), a.get("cash"),
                         a.get("buying_power")),))
        for a in body.get("accounts", ()))
    fx = tuple(FxRate(str(x["currency"]).upper(), str(x["base"]).upper(), float(x["rate"]), FX_SOURCE, at)
               for x in body.get("fx", ()) if x.get("rate") and str(x["currency"]).upper() != str(x["base"]).upper())
    return BrokerSnapshot(BROKER, at, accounts, holdings, fx)
