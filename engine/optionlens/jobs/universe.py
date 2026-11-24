"""Universe selection (top stocks by market cap + ETF list + indices) and symbol resolution."""
from __future__ import annotations

import asyncio
import re
from functools import lru_cache
from pathlib import Path

from ..analytics import markets
from ..config import get_settings
from ..models import normalize_symbol
from ..net import FetchError
from ..repo import symbols as symbols_repo
from ..sources import cboe, nasdaq
from . import universe_ca

INDICES = {"SPX": "S&P 500 Index", "NDX": "Nasdaq-100 Index", "RUT": "Russell 2000 Index",
           "XSP": "Mini-SPX Index"}
_CLASS_SHARE = re.compile(r"^[A-Z]+\.[A-Z]$")          # BRK.B (Nasdaq lists it as BRK/B)
_NOT_COMMON = re.compile(r"\b(warrants?|rights?|preferred)\b", re.I)
_UNITS = re.compile(r"\bunits?\b", re.I)
_LP_UNITS = re.compile(r"(common|partnership|partner) units?|units? representing", re.I)


class UnknownSymbol(LookupError):
    """No source knows the symbol (Nasdaq for US listings, Questrade for Canadian ones)."""


def is_common_stock(row: dict) -> bool:
    """Screener row is a common stock / ADR / LP unit (not a warrant, SPAC unit, right, preferred, note)."""
    sym, name = row["symbol"], row.get("name") or ""
    if not sym or "^" in sym or not (row.get("market_cap") or 0) > 0:
        return False
    if "." in sym and not _CLASS_SHARE.match(sym):
        return False
    if "%" in name or _NOT_COMMON.search(name):
        return False
    if _UNITS.search(name) and not _LP_UNITS.search(name):
        return False
    low = name.lower()
    return not ("depositary share" in low and "american depositary" not in low)


def select_top_stocks(rows: list[dict], n: int) -> list[dict]:
    """Top n common stocks by market cap."""
    common = [r for r in rows if is_common_stock(r)]
    return sorted(common, key=lambda r: r["market_cap"], reverse=True)[:n]


@lru_cache(maxsize=4)
def etf_list(path: Path | None = None) -> tuple[str, ...]:
    """Universe ETFs from etfs.txt (comments and blanks ignored)."""
    p = path or get_settings().etf_file
    out = []
    for line in p.read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            out.append(normalize_symbol(s))
    return tuple(dict.fromkeys(out))


async def universe() -> str:
    """Refresh symbols from the Nasdaq screeners and set universe membership."""
    s = get_settings()
    stocks = await nasdaq.fetch_stock_screener()
    if not stocks:
        raise RuntimeError("Nasdaq stock screener returned no rows")
    try:
        etf_rows = await nasdaq.fetch_etf_screener()
    except FetchError:
        etf_rows = []
    top = select_top_stocks(stocks, s.universe_stocks)
    etfs = etf_list()
    names = {r["symbol"]: r["name"] for r in etf_rows}
    rows = [{**r, "kind": "stock"} for r in stocks if is_common_stock(r)]
    rows += [{"symbol": r["symbol"], "name": r["name"], "kind": "etf"} for r in etf_rows]
    rows += [{"symbol": e, "name": names.get(e), "kind": "etf"} for e in etfs]
    rows += [{"symbol": k, "name": v, "kind": "index"} for k, v in INDICES.items()]
    members = [r["symbol"] for r in top] + list(etfs) + list(INDICES)

    def store() -> int:
        symbols_repo.upsert_symbols(rows)
        n = symbols_repo.set_universe(members)
        symbols_repo.reprobe_not_optionable(members)
        return n

    n = await asyncio.to_thread(store)
    return f"universe {n} ({len(top)} stocks, {len(etfs)} ETFs, {len(INDICES)} indices); {len({r['symbol'] for r in rows})} symbols known"


async def ensure_symbol(raw: str) -> dict:
    """symbols row for a symbol, creating it (kind via index list / etfs.txt / Nasdaq info; a Canadian symbol from
    Questrade) if unknown. Raises UnknownSymbol when the source does not know it either."""
    symbol = normalize_symbol(raw)
    row = await asyncio.to_thread(symbols_repo.get_symbol, symbol)
    if row:
        return row
    if markets.is_canadian(symbol):    # Nasdaq does not list Toronto symbols: Questrade does
        row = await universe_ca.ensure_canadian(symbol)
        if row is None:
            raise UnknownSymbol(symbol)
        return row
    if symbol in cboe.INDEX_SYMBOLS:
        new = {"symbol": symbol, "name": INDICES.get(symbol, symbol), "kind": "index"}
    else:
        new = None
        order = ("etf", "stocks") if symbol in etf_list() else ("stocks", "etf")
        for asset in order:
            info = await nasdaq.fetch_info(symbol, asset)  # type: ignore[arg-type]
            if info:
                new = {"symbol": symbol, "name": info["name"], "kind": "etf" if asset == "etf" else "stock"}
                break
        if new is None:
            raise UnknownSymbol(symbol)
    await asyncio.to_thread(symbols_repo.upsert_symbols, [new])
    return await asyncio.to_thread(symbols_repo.get_symbol, symbol)  # type: ignore[return-value]
