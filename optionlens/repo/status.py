"""Database-wide status figures for /api/status."""
from __future__ import annotations

from .. import db
from ._sql import scalar

TABLES = ("symbols", "daily_bars", "quotes_latest", "option_latest", "option_eod", "metrics_daily",
          "earnings", "dividends", "index_bars", "rates", "report_snapshots", "watchlists", "presets",
          "alerts", "notifications", "http_cache", "job_runs")


def row_counts() -> dict[str, int]:
    """-> {table: row count} for every table."""
    sql = " UNION ALL ".join(f"SELECT '{t}' AS t, count(*) AS n FROM {t}" for t in TABLES)
    return {t: int(n) for t, n in db.cursor().execute(sql).fetchall()}


def distinct_dates(table: str) -> int:
    """Number of distinct `date` values in metrics_daily / option_eod / daily_bars / report_snapshots."""
    if table not in ("metrics_daily", "option_eod", "daily_bars", "report_snapshots"):
        raise ValueError(table)
    return int(scalar(f"SELECT count(DISTINCT date) FROM {table}"))


def db_size_bytes() -> int:
    """Size of the database file (+ WAL) in bytes."""
    p = db.path()
    if p is None:
        return 0
    wal = p.with_name(p.name + ".wal")
    return sum(f.stat().st_size for f in (p, wal) if f.exists())
