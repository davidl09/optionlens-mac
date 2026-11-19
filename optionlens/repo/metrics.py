"""metrics_daily: one row per (date, symbol) of chain/vol metrics; today's row upserted every sweep."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Sequence

import numpy as np

from .. import db
from ._sql import columns, now_utc, query, scalar, split_by


@lru_cache(maxsize=1)
def metric_columns() -> tuple[str, ...]:
    """Value columns of metrics_daily (everything except date, symbol, updated_at), from the schema."""
    rows = db.cursor().execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'metrics_daily' "
        "ORDER BY ordinal_position").fetchall()
    return tuple(r[0] for r in rows if r[0] not in ("date", "symbol", "updated_at"))


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)) and not np.isfinite(v):
        return None
    return v.item() if isinstance(v, np.generic) else v


def upsert_metrics(day: date, symbol: str, values: dict) -> None:
    """Insert or replace the (day, symbol) row. Unknown keys raise ValueError; missing keys become NULL;
    NaN/inf stored as NULL."""
    cols = metric_columns()
    unknown = set(values) - set(cols)
    if unknown:
        raise ValueError(f"unknown metrics columns: {sorted(unknown)}")
    names = ["date", "symbol", *cols, "updated_at"]
    vals = [day, symbol, *(_clean(values.get(c)) for c in cols), now_utc()]
    with db.write() as cur:
        cur.execute(f"INSERT OR REPLACE INTO metrics_daily ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' * len(names))})", vals)


def metrics_on(day: date | None = None) -> dict[str, dict]:
    """All symbols' rows for day (default: latest stored date) -> {symbol: row dict}."""
    d = day or latest_date()
    if d is None:
        return {}
    return {r["symbol"]: r for r in query("SELECT * FROM metrics_daily WHERE date = ?", [d])}


def get_metrics(symbol: str, day: date | None = None) -> dict | None:
    """One symbol's row for day (default: its latest row) or None."""
    rows = query("""SELECT * FROM metrics_daily WHERE symbol = ? AND (?::DATE IS NULL OR date = ?)
                    ORDER BY date DESC LIMIT 1""", [symbol, day, day])
    return rows[0] if rows else None


def history(symbol: str, start: date | None = None, cols: Sequence[str] | None = None) -> dict[str, np.ndarray]:
    """One symbol's metrics since start -> {date, <cols or all metric columns>} ascending."""
    sel = _select(cols)
    return columns(f"""SELECT date, {sel} FROM metrics_daily WHERE symbol = ?
                       AND date >= COALESCE(?::DATE, DATE '1900-01-01') ORDER BY date""", [symbol, start])


def history_bulk(cols: Sequence[str], last_n: int = 252, symbols: Sequence[str] | None = None,
                 end: date | None = None) -> dict[str, dict[str, np.ndarray]]:
    """Last n stored rows (<= end) per symbol -> {symbol: {date, <cols>}} ascending (e.g. IV rank windows)."""
    syms = None if symbols is None else list(symbols)
    return split_by(columns(f"""
        SELECT symbol, date, {_select(cols)} FROM (
          SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn FROM metrics_daily
          WHERE date <= COALESCE(?::DATE, DATE '2999-12-31')
            AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[]))))
        WHERE rn <= ? ORDER BY symbol, date""", [end, syms, syms, last_n]), "symbol")


def series_between(start: date, end: date, cols: Sequence[str]) -> dict[str, np.ndarray]:
    """All symbols' rows in [start, end] -> columns {date, symbol, <cols>} ordered by date, symbol (e.g. market P/C history)."""
    return columns(f"""SELECT date, symbol, {_select(cols)} FROM metrics_daily
                       WHERE date BETWEEN ? AND ? ORDER BY date, symbol""", [start, end])


def latest_date() -> date | None:
    """Most recent metrics date."""
    return scalar("SELECT max(date) FROM metrics_daily")


def dates(symbol: str | None = None) -> list[date]:
    """Distinct metrics dates (optionally for one symbol), ascending."""
    return [r["date"] for r in query(
        "SELECT DISTINCT date FROM metrics_daily WHERE (?::VARCHAR IS NULL OR symbol = ?) ORDER BY date",
        [symbol, symbol])]


def _select(cols: Sequence[str] | None) -> str:
    allowed = metric_columns()
    cols = list(cols or allowed)
    bad = [c for c in cols if c not in allowed]
    if bad:
        raise ValueError(f"unknown metrics columns: {bad}")
    return ", ".join(cols)
