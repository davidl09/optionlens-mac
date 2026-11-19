"""dividends: ex-dates from the Nasdaq calendar and per-symbol histories."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Literal, Sequence

from .. import db
from ..models import Dividend
from ._sql import insert_arrow, query, to_columns

_FIELDS = ("symbol", "ex_date", "amount", "pay_date", "record_date", "declared_date", "kind")
_COLS = ", ".join(_FIELDS)


def upsert_dividends(divs: Iterable[Dividend]) -> int:
    """Insert or replace by (symbol, ex_date). Returns rows written."""
    uniq = list({(d.symbol, d.ex_date): d for d in divs}.values())
    with db.write() as cur:
        return insert_arrow(cur, "dividends", to_columns(uniq, _FIELDS), "INSERT OR REPLACE")


def for_symbol(symbol: str) -> list[Dividend]:
    """All stored dividends of a symbol, ascending by ex_date."""
    return [Dividend(**r) for r in query(
        f"SELECT {_COLS} FROM dividends WHERE symbol = ? ORDER BY ex_date", [symbol])]


def between(start: date, end: date, by: Literal["ex_date", "declared_date", "pay_date"] = "ex_date",
            symbols: Sequence[str] | None = None) -> list[Dividend]:
    """Dividends whose `by` date is in [start, end] (optionally for symbols), ordered by that date, symbol."""
    if by not in ("ex_date", "declared_date", "pay_date"):
        raise ValueError(by)
    syms = None if symbols is None else list(symbols)
    return [Dividend(**r) for r in query(f"""
        SELECT {_COLS} FROM dividends WHERE {by} BETWEEN ? AND ?
          AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
        ORDER BY {by}, symbol""", [start, end, syms, syms])]


def next_ex(on_or_after: date) -> dict[str, Dividend]:
    """First ex-date on/after a date per symbol -> {symbol: Dividend}."""
    return {r["symbol"]: Dividend(**r) for r in query(f"""
        SELECT {_COLS} FROM dividends WHERE ex_date >= ?
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ex_date) = 1""", [on_or_after])}


def trailing_sum(as_of: date, days: int = 365) -> dict[str, float]:
    """Sum of dividend amounts with ex_date in (as_of - days, as_of] per symbol -> {symbol: amount}."""
    rows = query("""SELECT symbol, sum(amount) AS amt FROM dividends
                    WHERE ex_date > ? AND ex_date <= ? GROUP BY symbol""", [as_of - timedelta(days=days), as_of])
    return {r["symbol"]: float(r["amt"]) for r in rows}


def symbols_with_history() -> set[str]:
    """Symbols having any stored dividend."""
    return {r["symbol"] for r in query("SELECT DISTINCT symbol FROM dividends")}
