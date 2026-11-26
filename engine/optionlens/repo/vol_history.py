"""vol_history: daily IV30 / HV30 series pushed by data clients (IBKR), and the merged IV30 history
(metrics_daily.iv30 where collected, else the ingested value) read by IV rank, vol charts and the
model-pricing IV proxy."""
from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

import numpy as np

from .. import db
from ._sql import columns, insert_arrow, now_utc, query, scalar, split_by
from .metrics import metric_columns

KINDS = ("iv30", "hv30")


def upsert(symbol: str, kind: str, source: str, points: Iterable[tuple[date, float]]) -> int:
    """Insert or replace (date, value) points of one symbol's series. Returns rows written."""
    if kind not in KINDS:
        raise ValueError(f"unknown vol_history kind {kind!r}")
    uniq = dict(points)
    n = len(uniq)
    data = {"symbol": [symbol] * n, "kind": [kind] * n, "source": [source] * n, "date": list(uniq),
            "value": [float(v) for v in uniq.values()], "updated_at": [now_utc()] * n}
    with db.write() as cur:
        return insert_arrow(cur, "vol_history", data, "INSERT OR REPLACE")


def last_dates(kind: str, symbols: Sequence[str] | None = None) -> dict[str, date]:
    """{symbol: latest ingested date} of a series kind (any source)."""
    syms = None if symbols is None else list(symbols)
    return {r["symbol"]: r["d"] for r in query(
        """SELECT symbol, max(date) AS d FROM vol_history WHERE kind = ?
           AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[]))) GROUP BY symbol""",
        [kind, syms, syms])}


def history_coverage(kind: str, symbols: Sequence[str], exclude_source_suffix: str) -> dict[str, tuple[date, date]]:
    """{symbol: (first, last) date} of a series kind over points whose source does not end with
    `exclude_source_suffix` (e.g. single-day values pushed with underlying stats)."""
    return {r["symbol"]: (r["f"], r["l"]) for r in query(
        """SELECT symbol, min(date) AS f, max(date) AS l FROM vol_history
           WHERE kind = ? AND NOT ends_with(source, ?) AND symbol IN (SELECT unnest(?::VARCHAR[]))
           GROUP BY symbol""", [kind, exclude_source_suffix, list(symbols)])}


def _merged(cols: Sequence[str]) -> str:
    """SELECT of (symbol, date, <cols>) over metrics_daily FULL JOIN ingested iv30, iv30 = collected
    else ingested (one source per date: the alphabetically first). Params: [syms, syms, syms, syms]."""
    allowed = metric_columns()
    bad = [c for c in cols if c not in allowed]
    if bad:
        raise ValueError(f"unknown metrics columns: {bad}")
    other = "".join(f", m.{c}" for c in cols if c != "iv30")
    return f"""
        SELECT COALESCE(m.symbol, h.symbol) AS symbol, COALESCE(m.date, h.date) AS date,
               COALESCE(m.iv30, h.v) AS iv30{other}
        FROM (SELECT * FROM metrics_daily
              WHERE ?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[]))) m
        FULL OUTER JOIN (SELECT symbol, date, arg_min(value, source) AS v FROM vol_history
              WHERE kind = 'iv30' AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
              GROUP BY symbol, date) h
        ON m.symbol = h.symbol AND m.date = h.date"""


def history_merged(symbol: str, cols: Sequence[str], start: date | None = None) -> dict[str, np.ndarray]:
    """metrics_repo.history shape ({date, <cols>} ascending) with iv30 filled from ingested history
    where metrics_daily has none; dates with only ingested iv30 carry NULL (NaN) other columns."""
    cols = list(cols)
    syms = [symbol]
    return columns(f"""SELECT date, {", ".join(cols)} FROM ({_merged(cols)})
                       WHERE date >= COALESCE(?::DATE, DATE '1900-01-01') ORDER BY date""",
                   [syms, syms, syms, syms, start])


def iv30_bulk(last_n: int, symbols: Sequence[str] | None = None,
              end: date | None = None) -> dict[str, dict[str, np.ndarray]]:
    """Last n merged iv30 rows (<= end) per symbol -> {symbol: {date, iv30}} ascending."""
    syms = None if symbols is None else list(symbols)
    return split_by(columns(f"""
        SELECT symbol, date, iv30 FROM (
          SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn FROM ({_merged(["iv30"])})
          WHERE date <= COALESCE(?::DATE, DATE '2999-12-31'))
        WHERE rn <= ? ORDER BY symbol, date""", [syms, syms, syms, syms, end, last_n]), "symbol")


def latest_iv30_date() -> date | None:
    """Most recent date of the merged history: any metrics_daily row or ingested iv30 point."""
    return scalar("""SELECT max(d) FROM (SELECT max(date) AS d FROM metrics_daily
                     UNION ALL SELECT max(date) FROM vol_history WHERE kind = 'iv30')""")


def latest_iv30_dates() -> dict[str, date]:
    """{symbol: the most recent date of its merged history} (any metrics_daily row or ingested iv30 point)."""
    return {r["symbol"]: r["d"] for r in query("""
        SELECT symbol, max(d) AS d FROM (SELECT symbol, max(date) AS d FROM metrics_daily GROUP BY symbol
            UNION ALL SELECT symbol, max(date) FROM vol_history WHERE kind = 'iv30' GROUP BY symbol)
        GROUP BY symbol""")}
