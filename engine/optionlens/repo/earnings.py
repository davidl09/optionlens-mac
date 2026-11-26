"""earnings: every calendar row a source gives (not only universe symbols), tagged with its source: Nasdaq for US
listings, TMX Money / Yahoo Finance for Canadian ones (EarningsEvent.source)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from .. import db
from ..models import EarningsEvent
from ._sql import insert_arrow, query, to_columns

_FIELDS = ("symbol", "date", "timing", "fiscal_quarter", "eps_actual", "eps_estimate", "surprise_pct",
           "num_estimates", "source")
_KEPT = _FIELDS[3:-1]   # values a new row may lack and the stored one keeps
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
        out.append(replace(e, **{f: getattr(prev, f) for f in _KEPT if getattr(e, f) is None},
                           timing=e.timing if e.timing != "unknown" else prev.timing))
    return out


def replace_day(day: date, events: Iterable[EarningsEvent], source: str) -> int:
    """Replace the events of `source` dated `day` with `events` (drops rescheduled ones; keeps known values the
    new rows lack; other sources' rows of that day stay unless `events` has the same symbol, which it replaces).
    Returns rows written."""
    uniq = list({e.symbol: e for e in events if e.date == day}.values())
    if any(e.source != source for e in uniq):
        raise ValueError(f"replace_day({day}, source={source!r}) given rows of another source")
    with db.write() as cur:
        rows = _merged(cur, uniq)
        cur.execute("DELETE FROM earnings WHERE date = ? AND (source = ? OR symbol IN (SELECT unnest(?::VARCHAR[])))",
                    [day, source, [e.symbol for e in uniq]])
        return insert_arrow(cur, "earnings", to_columns(rows, _FIELDS))


def upsert_earnings(events: Iterable[EarningsEvent]) -> int:
    """Insert or update events by (symbol, date), keeping known values the new rows lack. Returns rows written."""
    uniq = list({(e.symbol, e.date): e for e in events}.values())
    with db.write() as cur:
        return insert_arrow(cur, "earnings", to_columns(_merged(cur, uniq), _FIELDS), "INSERT OR REPLACE")


def replace_upcoming(events: Mapping[str, EarningsEvent], since: date, sources: Sequence[str]) -> int:
    """Each symbol's next event as a source selected it (sources/ca_earnings.py): the symbol's other rows dated on/after
    `since` from `sources` go (a moved date), then the event is upserted. Returns rows written."""
    if not events:
        return 0
    syms, dates = list(events), [e.date for e in events.values()]
    with db.write() as cur:
        rows = _merged(cur, list(events.values()))
        cur.execute("""DELETE FROM earnings WHERE date >= ? AND source IN (SELECT unnest(?::VARCHAR[]))
                         AND symbol IN (SELECT unnest(?::VARCHAR[]))""", [since, list(sources), syms])
        cur.execute("DELETE FROM earnings WHERE (symbol, date) IN (SELECT unnest(?::VARCHAR[]), unnest(?::DATE[]))",
                    [syms, dates])
        return insert_arrow(cur, "earnings", to_columns(rows, _FIELDS))


def has_events(symbol: str) -> bool:
    return bool(query("SELECT 1 FROM earnings WHERE symbol = ? LIMIT 1", [symbol]))


# ----------------------------------------------------------------------------- next-earnings lookups (Canada)
def record_lookups(rows: Iterable[tuple[str, datetime, str | None, str | None]]) -> int:
    """Store (symbol, checked_at, source | None, detail | None) of a lookup run (one row per symbol, latest wins)."""
    data = list({r[0]: r for r in rows}.values())
    with db.write() as cur:
        return insert_arrow(cur, "earnings_lookups", {k: [r[i] for r in data] for i, k in
                                                      enumerate(("symbol", "checked_at", "source", "detail"))},
                            "INSERT OR REPLACE")


def lookup(symbol: str) -> dict | None:
    """{symbol, checked_at, source, detail} of the symbol's latest lookup, or None."""
    rows = query("SELECT symbol, checked_at, source, detail FROM earnings_lookups WHERE symbol = ?", [symbol])
    return rows[0] if rows else None


def answered_since(since: datetime) -> set[str]:
    """Symbols a source answered in a lookup at/after `since` (the per-day cache of the lookup job)."""
    return {r["symbol"] for r in query(
        "SELECT symbol FROM earnings_lookups WHERE source IS NOT NULL AND checked_at >= ?", [since])}


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
