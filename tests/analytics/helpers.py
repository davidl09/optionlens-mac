"""Shared test helpers: a minimal local parser of the real CBOE fixture (the production parser
lives in sources/cboe.py) and small factories."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from optionlens.models import ChainSnapshot, OptionContract, Quote

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _f(x):
    return None if x is None else float(x)


def parse_osi(osi: str) -> tuple[str, date, str, float]:
    i = len(osi) - 15
    root, ymd, right, k = osi[:i], osi[i:i + 6], osi[i + 6], osi[i + 7:]
    return root, date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), right, int(k) / 1000.0


def load_aapl_chain() -> tuple[ChainSnapshot, dict]:
    raw = json.loads((FIXTURES / "cboe_chain_AAPL.json").read_text())
    d = raw["data"]
    ts = datetime.strptime(raw["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    q = Quote(symbol="AAPL", ts=ts, price=d["current_price"], change=d["price_change"],
              change_pct=d["price_change_percent"] / 100, open=d["open"], high=d["high"], low=d["low"],
              prev_close=d["prev_day_close"], volume=int(d["volume"]), bid=d["bid"], ask=d["ask"],
              iv30=d["iv30"] / 100)
    contracts = []
    for o in d["options"]:
        _, exp, right, k = parse_osi(o["option"])
        contracts.append(OptionContract(
            osi=o["option"], symbol="AAPL", expiry=exp, strike=k, right=right,
            bid=_f(o["bid"]), ask=_f(o["ask"]), last=_f(o["last_trade_price"]), change=_f(o["change"]),
            volume=int(o["volume"] or 0), oi=int(o["open_interest"] or 0),
            iv=_f(o["iv"]) if o["iv"] else None, delta=_f(o["delta"]), gamma=_f(o["gamma"]),
            theta=_f(o["theta"]), vega=_f(o["vega"]), rho=_f(o["rho"]), theo=_f(o["theo"])))
    return ChainSnapshot(quote=q, contracts=tuple(contracts), fetched_at=ts), raw


def contract(expiry: date, strike: float, right: str, bid: float, ask: float, *, iv=None, delta=None,
             volume=0, oi=0, last=None, symbol="TEST", gamma=None, theta=None, vega=None) -> OptionContract:
    osi = f"{symbol}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"
    return OptionContract(osi=osi, symbol=symbol, expiry=expiry, strike=float(strike), right=right,
                          bid=bid, ask=ask, last=last, change=None, volume=volume, oi=oi, iv=iv,
                          delta=delta, gamma=gamma, theta=theta, vega=vega, rho=None, theo=None)
