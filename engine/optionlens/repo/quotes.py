"""quotes_latest: last underlying quote per symbol."""
from __future__ import annotations

from typing import Iterable

from .. import db
from ..models import Quote
from ._sql import insert_arrow, query, scalar, to_columns, utc_naive

_FIELDS = ("symbol", "ts", "price", "change", "change_pct", "open", "high", "low", "prev_close",
           "volume", "bid", "ask", "iv30", "delayed")
_SELECT = ", ".join(_FIELDS)


def upsert_quotes(quotes: Iterable[Quote], source: str | None = None) -> int:
    """Insert or replace quotes by symbol, tagged with `source` (None: unknown). Returns rows written."""
    uniq = list({q.symbol: q for q in quotes}.values())
    data = to_columns(uniq, _FIELDS)
    data["ts"] = [utc_naive(t) for t in data["ts"]]
    data["source"] = [source] * len(uniq)
    with db.write() as cur:
        return insert_arrow(cur, "quotes_latest", data, "INSERT OR REPLACE")


def get_quote(symbol: str) -> Quote | None:
    """Latest stored quote (ts aware UTC) or None."""
    rows = query(f"SELECT {_SELECT} FROM quotes_latest WHERE symbol = ?", [symbol])
    return Quote(**rows[0]) if rows else None


def all_quotes() -> dict[str, Quote]:
    """-> {symbol: Quote} for every stored quote."""
    return {r["symbol"]: Quote(**r) for r in query(f"SELECT {_SELECT} FROM quotes_latest")}


def source_of(symbol: str) -> str | None:
    """`source` tag of the stored quote (None when unknown or no quote)."""
    return scalar("SELECT source FROM quotes_latest WHERE symbol = ?", [symbol])
