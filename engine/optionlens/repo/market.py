"""Market-wide series: index_bars (VIX family) and risk-free rates per currency (fractions): `rates` = USD 13-week
T-bill (Treasury), `rates_cad` = CAD 3-month T-bill (Bank of Canada)."""
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


RATE_TABLES = {"USD": "rates", "CAD": "rates_cad"}


def _table(currency: str) -> str:
    try:
        return RATE_TABLES[currency]
    except KeyError:
        raise ValueError(f"no risk-free rate series for {currency!r}") from None


def upsert_rates(rows: Iterable[tuple[date, float]], currency: str = "USD") -> int:
    """Insert or replace (date, rate) rows of `currency`'s series. Returns rows written."""
    uniq = dict(rows)
    with db.write() as cur:
        return insert_arrow(cur, _table(currency), {"date": list(uniq), "rate": list(uniq.values())},
                            "INSERT OR REPLACE")


def rate_on(day: date | None = None, currency: str = "USD") -> float | None:
    """Latest stored rate of `currency` on or before day (default: latest overall), or None."""
    return scalar(f"SELECT rate FROM {_table(currency)} WHERE date <= COALESCE(?::DATE, DATE '2999-12-31') "
                  "ORDER BY date DESC LIMIT 1", [day])


def last_rate_date(currency: str = "USD") -> date | None:
    """Date of the latest stored rate of `currency`, or None."""
    return scalar(f"SELECT max(date) FROM {_table(currency)}")


def rates_between(start: date, end: date, currency: str = "USD") -> list[dict]:
    """-> [{date, rate}] ascending in [start, end]."""
    return query(f"SELECT date, rate FROM {_table(currency)} WHERE date BETWEEN ? AND ? ORDER BY date", [start, end])
