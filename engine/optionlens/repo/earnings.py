"""earnings: every calendar row Nasdaq gives (not only universe symbols)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Iterable, Sequence

from .. import db
from ..models import EarningsEvent
from ._sql import insert_arrow, query, to_columns

_FIELDS = ("symbol", "date", "timing", "fiscal_quarter", "eps_actual", "eps_estimate", "surprise_pct",
           "num_estimates")
_COLS = ", ".join(_FIELDS)


def _merged(cur: Any, events: list[EarningsEvent]) -> list[EarningsEvent]:
    """Keep already-known values the new rows lack (Nasdaq drops the bmo/amc time and sometimes
    estimates once a date is in the past)."""
    if not events:
        return []
    old = {(r[0], r[1]): r for r in cur.execute(
        f"SELECT {_COLS} FROM earnings WHERE (symbol, date) IN (SELECT unnest(?::VARCHAR[]), unnest(?::DATE[]))",
        [[e.symbol for e in events], [e.date for e in events]]).fetchall()}
    out = []
    for e in events:
        o = old.get((e.symbol, e.date))
        if o is None:
            out.append(e)
            continue
        prev = EarningsEvent(*o)
        out.append(replace(e, **{f: getattr(prev, f) for f in _FIELDS[3:] if getattr(e, f) is None},
                           timing=e.timing if e.timing != "unknown" else prev.timing))
    return out


def replace_day(day: date, events: Iterable[EarningsEvent]) -> int:
    """Replace all events dated `day` with `events` (drops rescheduled ones; keeps known values the
    new rows lack). Returns rows written."""
    uniq = list({e.symbol: e for e in events if e.date == day}.values())
    with db.write() as cur:
        rows = _merged(cur, uniq)
        cur.execute("DELETE FROM earnings WHERE date = ?", [day])
        return insert_arrow(cur, "earnings", to_columns(rows, _FIELDS))


def upsert_earnings(events: Iterable[EarningsEvent]) -> int:
    """Insert or update events by (symbol, date), keeping known values the new rows lack. Returns rows written."""
    uniq = list({(e.symbol, e.date): e for e in events}.values())
    with db.write() as cur:
        return insert_arrow(cur, "earnings", to_columns(_merged(cur, uniq), _FIELDS), "INSERT OR REPLACE")


def for_symbol(symbol: str) -> list[EarningsEvent]:
    """All stored events of a symbol, ascending by date."""
    return [EarningsEvent(**r) for r in query(
        f"SELECT {_COLS} FROM earnings WHERE symbol = ? ORDER BY date", [symbol])]


def between(start: date, end: date, symbols: Sequence[str] | None = None) -> list[EarningsEvent]:
    """Events with start <= date <= end (optionally for symbols), ordered by date, symbol."""
    syms = None if symbols is None else list(symbols)
    return [EarningsEvent(**r) for r in query(f"""
        SELECT {_COLS} FROM earnings WHERE date BETWEEN ? AND ?
          AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
        ORDER BY date, symbol""", [start, end, syms, syms])]


def next_events(on_or_after: date) -> dict[str, EarningsEvent]:
    """First event on/after a date per symbol -> {symbol: EarningsEvent}."""
    return {r["symbol"]: EarningsEvent(**r) for r in query(f"""
        SELECT {_COLS} FROM earnings WHERE date >= ?
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date) = 1""", [on_or_after])}


def last_events(before: date) -> dict[str, EarningsEvent]:
    """Last event strictly before a date per symbol -> {symbol: EarningsEvent}."""
    return {r["symbol"]: EarningsEvent(**r) for r in query(f"""
        SELECT {_COLS} FROM earnings WHERE date < ?
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1""", [before])}
