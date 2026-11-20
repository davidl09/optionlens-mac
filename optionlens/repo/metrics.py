"""metrics_daily: one row per (date, symbol) of chain/vol metrics; today's row upserted every sweep.
`source` says who supplied the chain-derived values (see schema.sql)."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Mapping, Sequence

import numpy as np

from .. import db
from ._sql import columns, insert_arrow, now_utc, query, scalar, split_by

_NON_VALUE = ("date", "symbol", "updated_at", "source")


@lru_cache(maxsize=1)
def metric_columns() -> tuple[str, ...]:
    """Value columns of metrics_daily (everything except date, symbol, updated_at, source), from the schema."""
    rows = db.cursor().execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'metrics_daily' "
        "ORDER BY ordinal_position").fetchall()
    return tuple(r[0] for r in rows if r[0] not in _NON_VALUE)


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)) and not np.isfinite(v):
        return None
    return v.item() if isinstance(v, np.generic) else v


def _check(values: Mapping) -> None:
    unknown = set(values) - set(metric_columns())
    if unknown:
        raise ValueError(f"unknown metrics columns: {sorted(unknown)}")


def upsert_metrics(day: date, symbol: str, values: dict, source: str | None = None) -> None:
    """Insert or replace the (day, symbol) row, tagged `source`. Unknown keys raise ValueError; missing
    keys become NULL; NaN/inf stored as NULL."""
    upsert_metrics_many(day, {symbol: (values, source)})


def upsert_metrics_many(day: date, rows: Mapping[str, tuple[Mapping, str | None]]) -> int:
    """upsert_metrics for many symbols in one write: {symbol: (values, source)}. Returns rows written."""
    cols = metric_columns()
    for values, _ in rows.values():
        _check(values)
    syms = list(rows)
    data: dict[str, list] = {"date": [day] * len(syms), "symbol": syms}
    for c in cols:
        data[c] = [_clean(rows[s][0].get(c)) for s in syms]
    data["updated_at"] = [now_utc()] * len(syms)
    data["source"] = [rows[s][1] for s in syms]
    with db.write() as cur:
        return insert_arrow(cur, "metrics_daily", data, "INSERT OR REPLACE")


def source_of(day: date, symbol: str) -> str | None:
    """`source` of the (day, symbol) row (None when absent or unknown)."""
    return scalar("SELECT source FROM metrics_daily WHERE date = ? AND symbol = ?", [day, symbol])


def top_by_option_volume(n: int, last_n: int = 5, symbols: Sequence[str] | None = None) -> list[str]:
    """The n symbols (among `symbols`, default all) with the highest mean call+put volume over their
    last `last_n` stored rows, highest first."""
    syms = None if symbols is None else list(symbols)
    return [r["symbol"] for r in query("""
        SELECT symbol FROM (
          SELECT symbol, COALESCE(call_volume, 0) + COALESCE(put_volume, 0) AS v,
                 row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
          FROM metrics_daily WHERE (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
            AND (call_volume IS NOT NULL OR put_volume IS NOT NULL))
        WHERE rn <= ? GROUP BY symbol ORDER BY avg(v) DESC, symbol LIMIT ?""", [syms, syms, last_n, n])]


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
