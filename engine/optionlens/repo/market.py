"""Market-wide series: index_bars (VIX family) and rates (13-week T-bill, fraction)."""
from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

import numpy as np

from .. import db
from ..models import Bar
from ._sql import columns, insert_arrow, query, scalar, split_by, to_columns


def upsert_index_bars(bars: Iterable[Bar]) -> int:
    """Insert or replace index bars by (symbol, date). Returns rows written."""
    uniq = list({(b.symbol, b.date): b for b in bars}.values())
    data = to_columns(uniq, ("symbol", "date", "open", "high", "low", "close"))
    with db.write() as cur:
        return insert_arrow(cur, "index_bars", data, "INSERT OR REPLACE")


def index_bars(symbols: Sequence[str], start: date | None = None) -> dict[str, dict[str, np.ndarray]]:
    """-> {symbol: {date, open, high, low, close}} ascending since start."""
    return split_by(columns("""
        SELECT symbol, date, open, high, low, close FROM index_bars
        WHERE symbol IN (SELECT unnest(?::VARCHAR[])) AND date >= COALESCE(?::DATE, DATE '1900-01-01')
        ORDER BY symbol, date""", [list(symbols), start]), "symbol")


def index_coverage(symbols: Sequence[str]) -> dict[str, dict]:
    """-> {symbol: {first, last}} for index series that have any bars."""
    rows = query("""SELECT symbol, min(date) AS first, max(date) AS last FROM index_bars
                    WHERE symbol IN (SELECT unnest(?::VARCHAR[])) GROUP BY symbol""", [list(symbols)])
    return {r.pop("symbol"): r for r in rows}


def index_last_date(symbol: str) -> date | None:
    """Latest stored date for an index series."""
    return scalar("SELECT max(date) FROM index_bars WHERE symbol = ?", [symbol])


def upsert_rates(rows: Iterable[tuple[date, float]]) -> int:
    """Insert or replace (date, rate) rows. Returns rows written."""
    uniq = dict(rows)
    with db.write() as cur:
        return insert_arrow(cur, "rates", {"date": list(uniq), "rate": list(uniq.values())}, "INSERT OR REPLACE")


def rate_on(day: date | None = None) -> float | None:
    """Latest stored rate on or before day (default: latest overall), or None."""
    return scalar("SELECT rate FROM rates WHERE date <= COALESCE(?::DATE, DATE '2999-12-31') ORDER BY date DESC LIMIT 1",
                  [day])


def rates_between(start: date, end: date) -> list[dict]:
    """-> [{date, rate}] ascending in [start, end]."""
    return query("SELECT date, rate FROM rates WHERE date BETWEEN ? AND ? ORDER BY date", [start, end])
