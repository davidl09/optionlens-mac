"""DuckDB connection for the single OptionLens process.

One module-level connection; each thread gets its own `cursor()` (DuckDB's in-process concurrency
model). Writes go through `write()`, which serializes writers with a process-wide lock and wraps
them in a transaction, so concurrent upserts never hit DuckDB write-write conflicts.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

from .config import PACKAGE_DIR, get_settings

SCHEMA = PACKAGE_DIR / "schema.sql"

_con: duckdb.DuckDBPyConnection | None = None
_path: Path | None = None
_generation = 0
_open_lock = threading.Lock()
_write_lock = threading.RLock()
_local = threading.local()


def connect(path: Path | str | None = None) -> Path:
    """Open (creating if needed) the database and apply the schema. Returns the file path."""
    global _con, _path, _generation
    with _open_lock:
        if _con is not None:
            return _path  # type: ignore[return-value]
        p = Path(path) if path else get_settings().db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(p))
        con.execute(SCHEMA.read_text())
        _con, _path = con, p
        _generation += 1
        return p


def close() -> None:
    """Close the connection (cursors held by threads become invalid)."""
    global _con, _path
    with _open_lock:
        if _con is not None:
            _con.close()
        _con, _path = None, None


def path() -> Path | None:
    """Path of the open database, or None."""
    return _path


def cursor() -> duckdb.DuckDBPyConnection:
    """This thread's cursor (connects lazily with default settings)."""
    if _con is None:
        connect()
    cur = getattr(_local, "cur", None)
    if cur is None or getattr(_local, "gen", -1) != _generation:
        cur = _con.cursor()  # type: ignore[union-attr]
        _local.cur, _local.gen = cur, _generation
    return cur


@contextmanager
def write() -> Iterator[duckdb.DuckDBPyConnection]:
    """Serialized write transaction on this thread's cursor (commit on success, rollback on error)."""
    with _write_lock:
        cur = cursor()
        cur.execute("BEGIN TRANSACTION")
        try:
            yield cur
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        cur.execute("COMMIT")
