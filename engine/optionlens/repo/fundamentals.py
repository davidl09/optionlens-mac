"""fundamentals_snapshot: one reference snapshot per symbol per day (Questrade GET v1/symbols/:id)."""
from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime
from typing import Iterable, Sequence

from .. import db
from ..models import Fundamentals
from ._sql import insert_arrow, query, to_columns, utc_naive

_FIELDS = tuple(f.name for f in fields(Fundamentals))
_COLS = ", ".join(_FIELDS)


def upsert(snaps: Iterable[Fundamentals], fetched_at: datetime) -> int:
    """Insert or replace by (symbol, date). Returns rows written."""
    uniq = list({(f.symbol, f.date): f for f in snaps}.values())
    data = to_columns(uniq, _FIELDS)
    data["fetched_at"] = [utc_naive(fetched_at)] * len(uniq)
    with db.write() as cur:
        return insert_arrow(cur, "fundamentals_snapshot", data, "INSERT OR REPLACE")


def latest(symbols: Sequence[str] | None = None, on_or_before: date | None = None) -> dict[str, Fundamentals]:
    """{symbol: the newest snapshot (on/before a date)}."""
    syms = None if symbols is None else list(symbols)
    return {r["symbol"]: Fundamentals(**r) for r in query(f"""
        SELECT {_COLS} FROM fundamentals_snapshot
        WHERE (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[]))) AND (?::DATE IS NULL OR date <= ?)
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1""", [syms, syms, on_or_before, on_or_before])}


def stored_on(day: date) -> set[str]:
    """Symbols with a snapshot of `day`."""
    return {r["symbol"] for r in query("SELECT symbol FROM fundamentals_snapshot WHERE date = ?", [day])}
