"""dividends: Nasdaq's rows (`dividends`: calendar + per-symbol histories) and the other sources' (`dividends_derived`:
IBKR-adjusted, Questrade). Every reader sees the merged set: per (symbol, ex-date) the row of the first source in
analytics.dividends.SOURCE_PRIORITY, its missing dates filled from the others, `discrepancy` when the sources' amounts
differ by more than DISCREPANCY_TOLERANCE."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Literal, Sequence

from .. import db
from ..analytics.dividends import DISCREPANCY_TOLERANCE, SOURCE_PRIORITY
from ..models import Dividend
from ._sql import insert_arrow, query, to_columns, utc_naive

_FIELDS = ("symbol", "ex_date", "amount", "pay_date", "record_date", "declared_date", "kind")
_COLS = ", ".join(_FIELDS)
_MERGED_COLS = _COLS + ", source, discrepancy"
NASDAQ = SOURCE_PRIORITY[0]


def _prio(col: str) -> str:
    return "CASE " + " ".join(f"WHEN {col} = '{s}' THEN {i}" for i, s in enumerate(SOURCE_PRIORITY)) + \
        f" ELSE {len(SOURCE_PRIORITY)} END"


def _merged(where: str = "TRUE") -> str:
    """The merged set as a subquery; `where` filters the observations (columns symbol, ex_date, pay_date,
    declared_date) before merging."""
    return f"""(
        WITH obs AS (
            SELECT {_COLS}, '{NASDAQ}' AS source FROM dividends
            UNION ALL
            SELECT symbol, ex_date, amount, pay_date, record_date, declared_date, kind, source FROM dividends_derived),
        o AS (SELECT *, {_prio('source')} AS prio FROM obs WHERE {where})
        SELECT symbol, ex_date, arg_min(amount, prio) AS amount,
               arg_min(pay_date, prio) FILTER (WHERE pay_date IS NOT NULL) AS pay_date,
               arg_min(record_date, prio) FILTER (WHERE record_date IS NOT NULL) AS record_date,
               arg_min(declared_date, prio) FILTER (WHERE declared_date IS NOT NULL) AS declared_date,
               arg_min(kind, prio) AS kind, arg_min(source, prio) AS source,
               max(amount) - min(amount) > {DISCREPANCY_TOLERANCE} AS discrepancy
        FROM o GROUP BY symbol, ex_date)"""


def _rows(sql: str, params: list) -> list[Dividend]:
    return [Dividend(**r) for r in query(sql, params)]


# ----------------------------------------------------------------------------- writes
def upsert_dividends(divs: Iterable[Dividend]) -> int:
    """Nasdaq's rows: insert or replace by (symbol, ex_date). Returns rows written."""
    uniq = list({(d.symbol, d.ex_date): d for d in divs}.values())
    with db.write() as cur:
        return insert_arrow(cur, "dividends", to_columns(uniq, _FIELDS), "INSERT OR REPLACE")


def upsert_derived(divs: Iterable[Dividend], now: datetime) -> int:
    """Rows of another source (`source` set): insert or replace by (symbol, ex_date, source)."""
    uniq = list({(d.symbol, d.ex_date, d.source): d for d in divs}.values())
    if any(d.source in (None, NASDAQ) for d in uniq):
        raise ValueError("derived dividends need their own source")
    data = to_columns(uniq, ("symbol", "ex_date", "source", "amount", "pay_date", "kind", "record_date", "declared_date"))
    data["updated_at"] = [utc_naive(now)] * len(uniq)
    with db.write() as cur:
        return insert_arrow(cur, "dividends_derived", data, "INSERT OR REPLACE")


def replace_derived(symbol: str, source: str, start: date, end: date, divs: Sequence[Dividend], now: datetime) -> int:
    """`source`'s rows of `symbol` with ex-dates in [start, end] become `divs` (a re-fetch drops what it no longer
    shows). Returns rows written."""
    with db.write() as cur:
        cur.execute("DELETE FROM dividends_derived WHERE symbol = ? AND source = ? AND ex_date BETWEEN ? AND ?",
                    [symbol, source, start, end])
    return upsert_derived(divs, now) if divs else 0


def record_history_fetch(symbol: str, source: str, first: date | None, last: date | None, dividends: int,
                         now: datetime) -> None:
    with db.write() as cur:
        cur.execute("INSERT OR REPLACE INTO dividend_history_fetches VALUES (?, ?, ?, ?, ?, ?)",
                    [symbol, source, first, last, dividends, utc_naive(now)])


def history_fetches(symbols: Sequence[str]) -> dict[str, dict]:
    """{symbol: {first_date, last_date, dividends, fetched_at}} of the recorded dividend-history fetches."""
    return {r["symbol"]: r for r in query(
        "SELECT symbol, first_date, last_date, dividends, fetched_at FROM dividend_history_fetches "
        "WHERE symbol IN (SELECT unnest(?::VARCHAR[]))", [list(symbols)])}


# ----------------------------------------------------------------------------- merged reads
def for_symbol(symbol: str) -> list[Dividend]:
    """The symbol's merged dividends, ascending by ex_date."""
    return _rows(f"SELECT {_MERGED_COLS} FROM {_merged('symbol = ?')} ORDER BY ex_date", [symbol])


def between(start: date, end: date, by: Literal["ex_date", "declared_date", "pay_date"] = "ex_date",
            symbols: Sequence[str] | None = None) -> list[Dividend]:
    """Merged dividends whose `by` date is in [start, end] (optionally for symbols), ordered by that date, symbol."""
    if by not in ("ex_date", "declared_date", "pay_date"):
        raise ValueError(by)
    syms = None if symbols is None else list(symbols)
    scope = "(?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))"
    # ex-dates filter before merging (fast); a pay / declared date may come from another source's row: after it
    pre, pre_params = (f"{scope} AND ex_date BETWEEN ? AND ?", [syms, syms, start, end]) if by == "ex_date" \
        else (scope, [syms, syms])
    return _rows(f"SELECT {_MERGED_COLS} FROM {_merged(pre)} WHERE {by} BETWEEN ? AND ? ORDER BY {by}, symbol",
                 [*pre_params, start, end])


def next_ex(on_or_after: date) -> dict[str, Dividend]:
    """First merged ex-date on/after a date per symbol -> {symbol: Dividend}."""
    return {d.symbol: d for d in _rows(f"""
        SELECT {_MERGED_COLS} FROM {_merged('ex_date >= ?')}
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ex_date) = 1""", [on_or_after])}


def trailing_sum(as_of: date, days: int = 365) -> dict[str, float]:
    """Sum of merged dividend amounts with ex_date in (as_of - days, as_of] per symbol -> {symbol: amount}."""
    rows = query(f"SELECT symbol, sum(amount) AS amt FROM {_merged('ex_date > ? AND ex_date <= ?')} GROUP BY symbol",
                 [as_of - timedelta(days=days), as_of])
    return {r["symbol"]: float(r["amt"]) for r in rows}


def symbols_with_history(covering: date | None = None) -> set[str]:
    """Symbols having any stored dividend (any source); with ``covering``, only those whose earliest stored
    ex-date is on/before it (history reaches back that far)."""
    return {r["symbol"] for r in query(
        f"SELECT symbol FROM {_merged()} GROUP BY symbol HAVING ?::DATE IS NULL OR min(ex_date) <= ?",
        [covering, covering])}


def by_symbol(symbols: Sequence[str], start: date | None = None) -> dict[str, list[Dividend]]:
    """{symbol: merged dividends ascending} for many symbols in one query (ex-dates from `start`)."""
    out: dict[str, list[Dividend]] = {}
    for d in _rows(f"""SELECT {_MERGED_COLS} FROM {_merged('symbol IN (SELECT unnest(?::VARCHAR[])) AND (?::DATE IS NULL OR ex_date >= ?)')}
                      ORDER BY symbol, ex_date""", [list(symbols), start, start]):
        out.setdefault(d.symbol, []).append(d)
    return out
