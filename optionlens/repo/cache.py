"""http_cache: TTL cache of normalized remote payloads (fundamentals, news, insiders, profile...)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .. import db
from ._sql import aware, dumps, loads, now_utc, utc_naive


def cache_get(key: str, max_age_s: float) -> Any | None:
    """Cached payload if stored within max_age_s seconds, else None."""
    row = db.cursor().execute("SELECT payload FROM http_cache WHERE key = ? AND fetched_at >= ?",
                              [key, now_utc() - timedelta(seconds=max_age_s)]).fetchone()
    return loads(row[0]) if row else None


def cache_get_any(key: str) -> tuple[Any, datetime] | None:
    """Cached payload regardless of age -> (payload, fetched_at aware UTC) or None (stale fallback)."""
    row = db.cursor().execute("SELECT payload, fetched_at FROM http_cache WHERE key = ?", [key]).fetchone()
    return (loads(row[0]), aware(row[1])) if row else None


def cache_put(key: str, payload: Any) -> None:
    """Store/replace a JSON-serializable payload stamped now."""
    with db.write() as cur:
        cur.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)", [key, dumps(payload), now_utc()])


def prune_cache(before: datetime) -> int:
    """Delete entries fetched before `before`. Returns rows deleted."""
    with db.write() as cur:
        cur.execute("DELETE FROM http_cache WHERE fetched_at < ?", [utc_naive(before)])
        return cur.fetchone()[0]
