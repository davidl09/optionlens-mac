"""bridge_status: the IBKR bridge's latest heartbeat and latest ingest push (one row)."""
from __future__ import annotations

from datetime import datetime

from .. import db
from ._sql import one, utc_naive

_ROW = 1


def record_heartbeat(client: str | None, ibkr_connected: bool, account_type: str | None, at: datetime) -> None:
    with db.write() as cur:
        cur.execute("""INSERT INTO bridge_status (id, client, ibkr_connected, account_type, heartbeat_at)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET client = excluded.client,
                       ibkr_connected = excluded.ibkr_connected, account_type = excluded.account_type,
                       heartbeat_at = excluded.heartbeat_at""",
                    [_ROW, client, ibkr_connected, account_type, utc_naive(at)])


def record_push(at: datetime) -> None:
    """Note an ingest data push (any endpoint) at `at`."""
    with db.write() as cur:
        cur.execute("""INSERT INTO bridge_status (id, last_push_at) VALUES (?, ?)
                       ON CONFLICT (id) DO UPDATE SET last_push_at = excluded.last_push_at""",
                    [_ROW, utc_naive(at)])


def get_status() -> dict | None:
    """{client, ibkr_connected, account_type, heartbeat_at, last_push_at} (aware UTC) or None."""
    return one(db.cursor().execute(
        "SELECT client, ibkr_connected, account_type, heartbeat_at, last_push_at FROM bridge_status WHERE id = ?",
        [_ROW]))
