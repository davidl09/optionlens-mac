"""quotes_latest: last underlying quote per symbol."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .. import db
from ..models import Quote
from ._sql import insert_arrow, query, scalar, to_columns, utc_naive

_FIELDS = ("symbol", "ts", "price", "change", "change_pct", "open", "high", "low", "prev_close",
           "volume", "bid", "ask", "iv30", "delayed", "vwap")   # Quote.halted lives in halt_status (repo.halts)
_SELECT = ", ".join(_FIELDS)


def upsert_quotes(quotes: Iterable[Quote], source: str | None = None) -> int:
    """Insert or replace quotes by symbol, tagged with `source` (None: unknown). Returns rows written."""
    uniq = list({q.symbol: q for q in quotes}.values())
    data = to_columns(uniq, _FIELDS)
    data["ts"] = [utc_naive(t) for t in data["ts"]]
    data["source"] = [source] * len(uniq)
    with db.write() as cur:
        return insert_arrow(cur, "quotes_latest", data, "INSERT OR REPLACE")


MERGE_FIELDS = ("price", "change", "change_pct", "bid", "ask", "open", "high", "low", "prev_close", "volume", "vwap")


def merge_quotes(rows: Iterable[Mapping[str, Any]], source: str | None, delayed: bool | None) -> int:
    """Field-wise upsert: each row ({symbol, ts, <MERGE_FIELDS>...}) replaces the stored quote's fields it carries
    (not None), others are kept; a row older than the stored quote is ignored. Returns rows written."""
    stored = all_quotes()
    out = []
    for q in rows:
        old = stored.get(q["symbol"])
        if old is not None and old.ts > q["ts"]:
            continue
        base: dict[str, Any] = {f: None for f in Quote.__slots__} if old is None else \
            {f: getattr(old, f) for f in Quote.__slots__}
        base.update(symbol=q["symbol"], ts=q["ts"], delayed=delayed,
                    **{f: q.get(f) for f in MERGE_FIELDS if q.get(f) is not None})
        if q.get("volume") is not None:
            base["volume"] = int(q["volume"])
        out.append(Quote(**base))
    return upsert_quotes(out, source) if out else 0


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
