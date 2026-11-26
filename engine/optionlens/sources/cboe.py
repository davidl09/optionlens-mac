"""CBOE payload parsers (pure) and the index history CSV fetch.

CBOE is retired from collection (its CDN refuses this host and its website terms do not allow systematic
collection): chains come from the IBKR bridge (ingest) or an optional vendor ChainSource. What stays:
the chain/quote parsers (they turn the recorded fixtures into models for tests) and the history CSV
fetch used only by the manual `cli vix` command (never scheduled).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from ..analytics.calendar import NY
from ..models import Bar, ChainSnapshot, OptionContract, Quote
from .. import net
from .osi import INDEX_ROOTS, parse_osi, standard_roots  # noqa: F401  (re-exported)

BASE_HOST = "cdn.cboe.com"
BASE = f"https://{BASE_HOST}/api/global"


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _pos(v: Any) -> float | None:
    """Float, with 0/None meaning 'unknown'."""
    return float(v) if v else None


def _ts(data: dict, payload: dict) -> datetime:
    """As-of time: last trade (ET wall clock) or the payload timestamp (UTC)."""
    if data.get("last_trade_time"):
        return datetime.fromisoformat(data["last_trade_time"]).replace(tzinfo=NY)
    if payload.get("timestamp"):
        return datetime.fromisoformat(payload["timestamp"]).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def parse_quote(payload: dict, symbol: str) -> Quote:
    """CBOE quote/chain payload -> Quote (change_pct and iv30 converted to fractions)."""
    d = payload["data"]
    pct = d.get("price_change_percent")
    return Quote(
        symbol=symbol, ts=_ts(d, payload),
        price=_pos(d.get("current_price")) or _pos(d.get("close")),
        change=_f(d.get("price_change")),
        change_pct=None if pct is None else float(pct) / 100.0,
        open=_pos(d.get("open")), high=_pos(d.get("high")), low=_pos(d.get("low")),
        prev_close=_pos(d.get("prev_day_close")),
        volume=None if d.get("volume") is None else int(d["volume"]),
        bid=_pos(d.get("bid")), ask=_pos(d.get("ask")),
        iv30=None if not d.get("iv30") else float(d["iv30"]) / 100.0,
    )


def _contract(o: dict, symbol: str, roots: frozenset[str]) -> OptionContract | None:
    root, expiry, right, strike = parse_osi(o["option"])
    if root not in roots:
        return None
    return OptionContract(
        osi=o["option"], symbol=symbol, expiry=expiry, strike=strike, right=right,
        bid=_f(o.get("bid")), ask=_f(o.get("ask")),
        last=_pos(o.get("last_trade_price")), change=_f(o.get("change")),
        volume=None if o.get("volume") is None else int(o["volume"]),
        oi=None if o.get("open_interest") is None else int(o["open_interest"]),
        iv=_pos(o.get("iv")),
        delta=_f(o.get("delta")), gamma=_f(o.get("gamma")), theta=_f(o.get("theta")),
        vega=_f(o.get("vega")), rho=_f(o.get("rho")), theo=_f(o.get("theo")),
    )


def parse_chain(payload: dict, symbol: str, fetched_at: datetime | None = None) -> ChainSnapshot:
    """CBOE options payload -> ChainSnapshot of standard contracts only (symbol = canonical requested symbol)."""
    opts = payload["data"].get("options") or []
    roots = standard_roots(symbol)
    return ChainSnapshot(
        quote=parse_quote(payload, symbol),
        contracts=tuple(c for c in (_contract(o, symbol, roots) for o in opts) if c is not None),
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )


def parse_history_csv(text: str, symbol: str) -> list[Bar]:
    """CBOE daily_prices CSV (DATE,OPEN,HIGH,LOW,CLOSE or close-only DATE,<SYM>) -> bars."""
    rows = csv.reader(io.StringIO(text))
    header = [h.strip().upper() for h in next(rows, [])]
    if not header or header[0] != "DATE":
        raise ValueError(f"unexpected CBOE CSV header {header!r}")
    ohlc = header[1:5] == ["OPEN", "HIGH", "LOW", "CLOSE"]
    out: list[Bar] = []
    for r in rows:
        if len(r) < 2 or not r[1].strip():
            continue
        d = datetime.strptime(r[0].strip(), "%m/%d/%Y").date()
        if ohlc:
            o, h, l, c = (float(x) for x in r[1:5])
            out.append(Bar(symbol, d, o, h, l, c, None))
        else:
            out.append(Bar(symbol, d, None, None, None, float(r[1]), None))
    return out


async def fetch_history(symbol: str) -> list[Bar]:
    """Fetch the full daily history CSV (VIX family, SPX, RUT, XSP; NDX is not published)."""
    text = await net.get_text(f"{BASE}/us_indices/daily_prices/{symbol}_History.csv")
    return parse_history_csv(text, symbol)
