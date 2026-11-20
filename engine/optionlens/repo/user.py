"""User data: watchlists, report presets, alerts (+ per-symbol cooldown state), notifications.

Dict shapes match web/src/api/types.ts (Watchlist, Preset, Alert, Notification); datetimes aware UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from .. import db
from ._sql import aware, dumps, loads, now_utc, query, utc_naive


# ----------------------------------------------------------------------------- watchlists
def list_watchlists() -> list[dict]:
    """-> [{id, name, symbols, created_at}] ordered by id; symbols in insertion order."""
    return query("""
        SELECT w.id, w.name,
               COALESCE(list(i.symbol ORDER BY i.added_at, i.symbol) FILTER (WHERE i.symbol IS NOT NULL), []) AS symbols,
               w.created_at
        FROM watchlists w LEFT JOIN watchlist_items i ON i.watchlist_id = w.id
        GROUP BY w.id, w.name, w.created_at ORDER BY w.id""")


def get_watchlist(watchlist_id: int) -> dict | None:
    """-> {id, name, symbols, created_at} or None."""
    return next((w for w in list_watchlists() if w["id"] == watchlist_id), None)


def create_watchlist(name: str, symbols: Iterable[str] = ()) -> dict:
    """Create a watchlist (ValueError if the name exists). Returns it."""
    with db.write() as cur:
        if cur.execute("SELECT 1 FROM watchlists WHERE name = ?", [name]).fetchone():
            raise ValueError(f"watchlist {name!r} exists")
        wid = cur.execute("INSERT INTO watchlists (name, created_at) VALUES (?, ?) RETURNING id",
                          [name, now_utc()]).fetchone()[0]
        _add_items(cur, wid, symbols)
    return get_watchlist(wid)  # type: ignore[return-value]


def update_watchlist(watchlist_id: int, name: str | None = None, symbols: Iterable[str] | None = None) -> dict | None:
    """Rename and/or replace symbols. Returns the watchlist or None if missing."""
    with db.write() as cur:
        if not cur.execute("SELECT 1 FROM watchlists WHERE id = ?", [watchlist_id]).fetchone():
            return None
        if name is not None:
            cur.execute("UPDATE watchlists SET name = ? WHERE id = ?", [name, watchlist_id])
        if symbols is not None:
            cur.execute("DELETE FROM watchlist_items WHERE watchlist_id = ?", [watchlist_id])
            _add_items(cur, watchlist_id, symbols)
    return get_watchlist(watchlist_id)


def delete_watchlist(watchlist_id: int) -> bool:
    """Delete a watchlist and its items. Returns whether it existed."""
    with db.write() as cur:
        cur.execute("DELETE FROM watchlist_items WHERE watchlist_id = ?", [watchlist_id])
        cur.execute("DELETE FROM watchlists WHERE id = ?", [watchlist_id])
        return cur.fetchone()[0] > 0


def add_symbol(watchlist_id: int, symbol: str) -> bool:
    """Add a symbol (idempotent). Returns False if the watchlist does not exist."""
    with db.write() as cur:
        if not cur.execute("SELECT 1 FROM watchlists WHERE id = ?", [watchlist_id]).fetchone():
            return False
        _add_items(cur, watchlist_id, [symbol])
    return True


def remove_symbol(watchlist_id: int, symbol: str) -> bool:
    """Remove a symbol. Returns whether it was present."""
    with db.write() as cur:
        cur.execute("DELETE FROM watchlist_items WHERE watchlist_id = ? AND symbol = ?", [watchlist_id, symbol])
        return cur.fetchone()[0] > 0


def watchlist_symbols(watchlist_id: int | None = None) -> list[str]:
    """Symbols of one watchlist, or the union of all when None."""
    return [r["symbol"] for r in query(
        "SELECT DISTINCT symbol FROM watchlist_items WHERE (?::INTEGER IS NULL OR watchlist_id = ?) ORDER BY symbol",
        [watchlist_id, watchlist_id])]


def _add_items(cur: Any, wid: int, symbols: Iterable[str]) -> None:
    """Insert symbols with strictly increasing added_at (1 µs apart, after any existing item) so
    list order = insertion order even within one call."""
    last = cur.execute("SELECT max(added_at) FROM watchlist_items WHERE watchlist_id = ?", [wid]).fetchone()[0]
    ts = now_utc()
    if last is not None and ts <= last:
        ts = last + timedelta(microseconds=1)
    for i, s in enumerate(dict.fromkeys(symbols)):
        cur.execute("INSERT INTO watchlist_items VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                    [wid, s, ts + timedelta(microseconds=i)])


# ----------------------------------------------------------------------------- presets
def _preset(r: dict) -> dict:
    return {**r, "payload": loads(r["payload"])}


def list_presets(report_id: str | None = None) -> list[dict]:
    """-> [{id, report_id, name, payload, created_at}] ordered by report_id, name."""
    return [_preset(r) for r in query(
        "SELECT * FROM presets WHERE (?::VARCHAR IS NULL OR report_id = ?) ORDER BY report_id, name",
        [report_id, report_id])]


def save_preset(report_id: str, name: str, payload: dict) -> dict:
    """Create, or overwrite the payload of the same (report_id, name). Returns the preset."""
    with db.write() as cur:
        cur.execute("""INSERT INTO presets (report_id, name, payload, created_at) VALUES (?, ?, ?, ?)
                       ON CONFLICT (report_id, name) DO UPDATE SET payload = excluded.payload""",
                    [report_id, name, dumps(payload), now_utc()])
    return next(p for p in list_presets(report_id) if p["name"] == name)


def delete_preset(preset_id: int) -> bool:
    """Delete a preset. Returns whether it existed."""
    with db.write() as cur:
        cur.execute("DELETE FROM presets WHERE id = ?", [preset_id])
        return cur.fetchone()[0] > 0


# ----------------------------------------------------------------------------- alerts
_ALERT_FIELDS = ("kind", "symbol", "params", "enabled", "cooldown_min")


def _alert(r: dict) -> dict:
    return {**r, "params": loads(r["params"]) or {}}


def list_alerts(enabled: bool | None = None) -> list[dict]:
    """-> [{id, kind, symbol, params, enabled, cooldown_min, last_fired_at, created_at}] ordered by id."""
    return [_alert(r) for r in query(
        "SELECT * FROM alerts WHERE (?::BOOLEAN IS NULL OR enabled = ?) ORDER BY id", [enabled, enabled])]


def get_alert(alert_id: int) -> dict | None:
    """One alert or None."""
    rows = query("SELECT * FROM alerts WHERE id = ?", [alert_id])
    return _alert(rows[0]) if rows else None


def create_alert(kind: str, symbol: str | None, params: dict | None = None, enabled: bool = True,
                 cooldown_min: int = 240) -> dict:
    """Create an alert. Returns it."""
    with db.write() as cur:
        aid = cur.execute("""INSERT INTO alerts (kind, symbol, params, enabled, cooldown_min, created_at)
                             VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
                          [kind, symbol, dumps(params or {}), enabled, cooldown_min, now_utc()]).fetchone()[0]
    return get_alert(aid)  # type: ignore[return-value]


def update_alert(alert_id: int, **fields: Any) -> dict | None:
    """Update any of kind, symbol, params, enabled, cooldown_min. Returns the alert or None if missing."""
    bad = set(fields) - set(_ALERT_FIELDS)
    if bad:
        raise ValueError(f"unknown alert fields: {sorted(bad)}")
    if fields:
        vals = [dumps(v) if k == "params" else v for k, v in fields.items()]
        with db.write() as cur:
            cur.execute(f"UPDATE alerts SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        [*vals, alert_id])
    return get_alert(alert_id)


def delete_alert(alert_id: int) -> bool:
    """Delete an alert and its cooldown state. Returns whether it existed."""
    with db.write() as cur:
        cur.execute("DELETE FROM alert_fires WHERE alert_id = ?", [alert_id])
        cur.execute("DELETE FROM alerts WHERE id = ?", [alert_id])
        return cur.fetchone()[0] > 0


def alert_fired_at(alert_id: int) -> dict[str, datetime]:
    """Per-symbol last fire time of an alert -> {symbol: aware UTC datetime}."""
    rows = db.cursor().execute("SELECT symbol, fired_at FROM alert_fires WHERE alert_id = ?", [alert_id]).fetchall()
    return {s: aware(t) for s, t in rows}


def record_alert_fire(alert_id: int, symbol: str, ts: datetime | None = None) -> None:
    """Record a fire for (alert, symbol) and bump alerts.last_fired_at."""
    t = utc_naive(ts) or now_utc()
    with db.write() as cur:
        cur.execute("INSERT OR REPLACE INTO alert_fires VALUES (?, ?, ?)", [alert_id, symbol, t])
        cur.execute("UPDATE alerts SET last_fired_at = ? WHERE id = ?", [t, alert_id])


# ----------------------------------------------------------------------------- notifications
def add_notification(title: str, body: str, link: str | None = None, alert_id: int | None = None,
                     ts: datetime | None = None) -> int:
    """Insert an unread notification. Returns its id."""
    with db.write() as cur:
        return cur.execute("""INSERT INTO notifications (ts, alert_id, title, body, link)
                              VALUES (?, ?, ?, ?, ?) RETURNING id""",
                           [utc_naive(ts) or now_utc(), alert_id, title, body, link]).fetchone()[0]


def list_notifications(unread_only: bool = False, limit: int = 200) -> list[dict]:
    """-> [{id, ts, alert_id, title, body, link, read}] newest first."""
    return query(f"SELECT * FROM notifications {'WHERE NOT read' if unread_only else ''} "
                 "ORDER BY ts DESC, id DESC LIMIT ?", [limit])


def mark_read(ids: Iterable[int] | None = None) -> int:
    """Mark given (or all) notifications read. Returns rows changed."""
    with db.write() as cur:
        if ids is None:
            cur.execute("UPDATE notifications SET read = true WHERE NOT read")
        else:
            cur.execute("UPDATE notifications SET read = true WHERE NOT read AND id IN (SELECT unnest(?::INTEGER[]))",
                        [list(ids)])
        return cur.fetchone()[0]


def unread_count() -> int:
    """Number of unread notifications."""
    return db.cursor().execute("SELECT count(*) FROM notifications WHERE NOT read").fetchone()[0]


def prune_notifications(before: datetime) -> int:
    """Delete notifications older than `before`. Returns rows deleted."""
    with db.write() as cur:
        cur.execute("DELETE FROM notifications WHERE ts < ?", [utc_naive(before)])
        return cur.fetchone()[0]
