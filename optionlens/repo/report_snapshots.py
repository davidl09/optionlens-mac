"""report_snapshots: per-day stored report rows (e.g. unusual-option-volume history)."""
from __future__ import annotations

from datetime import date

from .. import db
from ._sql import dumps, insert_arrow, loads, query


def write_snapshots(day: date, report_id: str, rows: list[dict]) -> int:
    """Replace (day, report_id) snapshot with rows (each must have 'symbol'). Returns rows written."""
    uniq = {r["symbol"]: r for r in rows}
    with db.write() as cur:
        cur.execute("DELETE FROM report_snapshots WHERE date = ? AND report_id = ?", [day, report_id])
        return insert_arrow(cur, "report_snapshots", {
            "date": [day] * len(uniq), "report_id": [report_id] * len(uniq),
            "symbol": list(uniq), "payload": [dumps(r) for r in uniq.values()]})


def read_snapshots(report_id: str, start: date, end: date | None = None,
                   symbols: list[str] | None = None) -> list[dict]:
    """-> [{date, symbol, payload}] for start <= date <= end (default start), ordered by date desc, symbol."""
    return [{**r, "payload": loads(r["payload"])} for r in query("""
        SELECT date, symbol, payload FROM report_snapshots
        WHERE report_id = ? AND date BETWEEN ? AND ?
          AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
        ORDER BY date DESC, symbol""", [report_id, start, end or start, symbols, symbols])]


def snapshot_dates(report_id: str) -> list[date]:
    """Distinct snapshot dates of a report, ascending."""
    return [r["date"] for r in query(
        "SELECT DISTINCT date FROM report_snapshots WHERE report_id = ? ORDER BY date", [report_id])]
