"""symbol_views: when the user last opened each symbol (feeds the bridge's chain universe)."""
from __future__ import annotations

from datetime import datetime

from .. import db
from ._sql import query, utc_naive


def record_view(symbol: str, at: datetime) -> None:
    with db.write() as cur:
        cur.execute("INSERT OR REPLACE INTO symbol_views VALUES (?, ?)", [symbol, utc_naive(at)])


def viewed_since(since: datetime) -> list[str]:
    """Symbols opened at/after `since`, most recent first."""
    return [r["symbol"] for r in query(
        "SELECT symbol FROM symbol_views WHERE viewed_at >= ? ORDER BY viewed_at DESC, symbol", [utc_naive(since)])]
