"""CBOE delayed quotes/chains and index history CSVs -> models. Parsers are pure; fetchers do I/O."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from datetime import date, datetime, timezone
from typing import Any

from ..analytics.calendar import NY
from ..models import Bar, ChainSnapshot, OptionContract, Quote, Right
from .. import net

BASE_HOST = "cdn.cboe.com"
BASE = f"https://{BASE_HOST}/api/global"
# Tickers CBOE serves with a leading underscore (cash-settled indices).
INDEX_SYMBOLS = frozenset({
    "SPX", "NDX", "RUT", "XSP", "DJX", "OEX", "XEO", "MRUT",
    "VIX", "VIX1D", "VIX9D", "VIX3M", "VIX6M",
})
# OSI roots carrying standard 100-share / index-multiplier contracts. Everything else CBOE lists in a
# chain (adjusted deliverables like AAPL1, B1SPY-style products, SPXO duplicates) is dropped at parse.
INDEX_ROOTS = {
    "SPX": frozenset({"SPX", "SPXW"}), "NDX": frozenset({"NDX", "NDXP"}), "RUT": frozenset({"RUT", "RUTW"}),
    "VIX": frozenset({"VIX", "VIXW"}),
}
_OSI = re.compile(r"^([A-Z0-9.]+?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def ticker(symbol: str) -> str:
    """CBOE ticker for a canonical symbol (SPX -> _SPX)."""
    return f"_{symbol}" if symbol in INDEX_SYMBOLS else symbol


def standard_roots(symbol: str) -> frozenset[str]:
    """OSI roots of a symbol's standard contracts (BRK.B -> BRKB; SPX -> SPX, SPXW)."""
    return INDEX_ROOTS.get(symbol) or frozenset({symbol.replace(".", "")})


def parse_osi(osi: str) -> tuple[str, date, Right, float]:
    """OSI code -> (root, expiry, right, strike). Raises ValueError on malformed codes."""
    m = _OSI.match(osi)
    if not m:
        raise ValueError(f"bad OSI code: {osi!r}")
    root, yy, mm, dd, right, strike = m.groups()
    return root, date(2000 + int(yy), int(mm), int(dd)), right, int(strike) / 1000.0  # type: ignore[return-value]


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
        volume=int(o.get("volume") or 0), oi=int(o.get("open_interest") or 0),
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


async def fetch_chain(symbol: str) -> ChainSnapshot:
    """Fetch the delayed option chain (FetchError 403/404 for unknown symbols)."""
    r = await net.get(f"{BASE}/delayed_quotes/options/{ticker(symbol)}.json")
    fetched_at = datetime.now(timezone.utc)
    # Large chains (SPX ~20 MB JSON): decode + build models off the event loop.
    return await asyncio.to_thread(lambda: parse_chain(json.loads(r.content), symbol, fetched_at))


async def fetch_quote(symbol: str) -> Quote:
    """Fetch the delayed quote only."""
    payload = await net.get_json(f"{BASE}/delayed_quotes/quotes/{ticker(symbol)}.json")
    return parse_quote(payload, symbol)


async def fetch_history(symbol: str) -> list[Bar]:
    """Fetch the full daily history CSV (VIX family, SPX, RUT, XSP; NDX is not published)."""
    text = await net.get_text(f"{BASE}/us_indices/daily_prices/{symbol}_History.csv")
    return parse_history_csv(text, symbol)
