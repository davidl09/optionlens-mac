"""daily_bars: per-symbol daily OHLCV. Columnar reads return {column: ndarray} sorted by date."""
from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

import numpy as np

from .. import db
from ..models import Bar
from ._sql import columns, insert_arrow, query, split_by, to_columns

_FIELDS = ("symbol", "date", "open", "high", "low", "close", "volume")
_COLS = "date, open, high, low, close, volume"


def upsert_bars(bars: Iterable[Bar], replace: bool = True) -> int:
    """Insert bars keyed by (symbol, date); existing rows are replaced, or kept when replace=False.
    Returns rows offered."""
    uniq = list({(b.symbol, b.date): b for b in bars}.values())
    with db.write() as cur:
        return insert_arrow(cur, "daily_bars", to_columns(uniq, _FIELDS),
                            "INSERT OR REPLACE" if replace else "INSERT OR IGNORE")


def upsert_closes(bars: Iterable[Bar]) -> int:
    """Close-only bars (sources without OHLC, e.g. CBOE SPX/RUT history): set the close of stored
    (symbol, date) rows, keeping their open/high/low/volume; insert dates not stored yet. Returns rows offered."""
    uniq = list({(b.symbol, b.date): b for b in bars}.values())
    with db.write() as cur:
        cur.execute("CREATE OR REPLACE TEMP TABLE _closes (symbol VARCHAR, date DATE, close DOUBLE)")
        n = insert_arrow(cur, "_closes", to_columns(uniq, ("symbol", "date", "close")))
        cur.execute("""INSERT INTO daily_bars (symbol, date, close) SELECT symbol, date, close FROM _closes
                       ON CONFLICT (symbol, date) DO UPDATE SET close = EXCLUDED.close""")
        cur.execute("DROP TABLE _closes")   # (rolled back with the transaction on error)
        return n


def replace_bars(symbol: str, bars: Iterable[Bar]) -> int:
    """Replace a symbol's whole history (used after a split is detected). Returns rows written."""
    uniq = list({b.date: b for b in bars if b.symbol == symbol}.values())
    with db.write() as cur:
        cur.execute("DELETE FROM daily_bars WHERE symbol = ?", [symbol])
        return insert_arrow(cur, "daily_bars", to_columns(uniq, _FIELDS))


def get_bars(symbol: str, start: date | None = None, end: date | None = None) -> dict[str, np.ndarray]:
    """-> {date, open, high, low, close, volume} ascending for symbol within [start, end]."""
    return columns(f"""SELECT {_COLS} FROM daily_bars WHERE symbol = ?
                       AND date >= COALESCE(?, DATE '1900-01-01') AND date <= COALESCE(?, DATE '2999-12-31')
                       ORDER BY date""", [symbol, start, end])


def bars_since(start: date, symbols: Sequence[str] | None = None) -> dict[str, dict[str, np.ndarray]]:
    """Bulk bars from start for all (or given) symbols -> {symbol: {date, open, high, low, close, volume}}."""
    return split_by(columns(f"""SELECT symbol, {_COLS} FROM daily_bars WHERE date >= ?
                                AND (?::VARCHAR[] IS NULL OR list_contains(?::VARCHAR[], symbol))
                                ORDER BY symbol, date""", [start, _list(symbols), _list(symbols)]), "symbol")


def last_closes(n: int, symbols: Sequence[str] | None = None) -> dict[str, dict[str, np.ndarray]]:
    """Last n bars per symbol -> {symbol: {date, close, volume}} ascending."""
    return split_by(columns("""
        SELECT symbol, date, close, volume FROM (
          SELECT symbol, date, close, volume,
                 row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
          FROM daily_bars WHERE (?::VARCHAR[] IS NULL OR list_contains(?::VARCHAR[], symbol)))
        WHERE rn <= ? ORDER BY symbol, date""", [_list(symbols), _list(symbols), n]), "symbol")


def coverage(symbols: Sequence[str] | None = None) -> dict[str, dict]:
    """-> {symbol: {count, first, last}} for symbols that have any bars."""
    rows = query("""SELECT symbol, count(*) AS count, min(date) AS first, max(date) AS last
                    FROM daily_bars WHERE (?::VARCHAR[] IS NULL OR list_contains(?::VARCHAR[], symbol))
                    GROUP BY symbol""", [_list(symbols), _list(symbols)])
    return {r.pop("symbol"): r for r in rows}


def bars_on(day: date) -> dict[str, Bar]:
    """All bars for one date -> {symbol: Bar}."""
    rows = query(f"SELECT symbol, {_COLS} FROM daily_bars WHERE date = ?", [day])
    return {r["symbol"]: Bar(**r) for r in rows}


def _list(symbols: Sequence[str] | None) -> list[str] | None:
    return None if symbols is None else list(symbols)


def month_end_closes(start: date, symbols: Sequence[str] | None = None) -> dict[str, np.ndarray]:
    """Last close of every (symbol, calendar month) since start -> columns (symbol, year, month, close)
    ordered by symbol, year, month."""
    return columns("""
        SELECT symbol, year(date) AS year, month(date) AS month, arg_max(close, date) AS close
        FROM daily_bars WHERE date >= ? AND (?::VARCHAR[] IS NULL OR list_contains(?::VARCHAR[], symbol))
        GROUP BY symbol, year(date), month(date) ORDER BY symbol, year, month""",
        [start, _list(symbols), _list(symbols)])
