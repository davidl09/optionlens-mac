"""Small helpers shared by repo modules (conversion between DuckDB results and plain Python)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

import numpy as np
import pyarrow as pa

from .. import db


def utc_naive(ts: datetime | None) -> datetime | None:
    """Aware datetime -> naive UTC for storage (naive input is assumed UTC already)."""
    if ts is None or ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def aware(ts: datetime | None) -> datetime | None:
    """Stored naive UTC -> aware UTC."""
    return None if ts is None else ts.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def dicts(cur: Any) -> list[dict]:
    """All rows of the last query as dicts (TIMESTAMPs made aware UTC)."""
    names = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        out.append({k: (aware(v) if isinstance(v, datetime) else v) for k, v in zip(names, row)})
    return out


def one(cur: Any) -> dict | None:
    """First row as dict or None."""
    rows = dicts(cur)
    return rows[0] if rows else None


def query(sql: str, params: Sequence[Any] | dict | None = None) -> list[dict]:
    """Run a read query on this thread's cursor -> list of dicts."""
    cur = db.cursor()
    cur.execute(sql, params or [])
    return dicts(cur)


def scalar(sql: str, params: Sequence[Any] | dict | None = None) -> Any:
    """Run a read query -> first column of first row (or None)."""
    r = db.cursor().execute(sql, params or []).fetchone()
    return r[0] if r else None


def columns(sql: str, params: Sequence[Any] | dict | None = None) -> dict[str, np.ndarray]:
    """Run a read query -> {column: ndarray}. Numeric NULL -> NaN (ints with NULLs become float),
    DATE -> datetime64[D], TIMESTAMP -> datetime64[us] (UTC), strings/bools -> object with None."""
    cur = db.cursor()
    cur.execute(sql, params or [])
    types = {d[0]: str(d[1]) for d in cur.description}
    out: dict[str, np.ndarray] = {}
    for k, v in cur.fetchnumpy().items():
        if isinstance(v, np.ma.MaskedArray):
            kind = v.dtype.kind
            if kind in "fiu":
                v = v.astype(np.float64).filled(np.nan)
            elif kind == "M":
                v = v.filled(np.datetime64("NaT"))
            else:
                v = v.astype(object).filled(None)
        v = np.asarray(v)
        if types.get(k) == "DATE":
            v = v.astype("datetime64[D]")
        out[k] = v
    return out


def split_by(cols: dict[str, np.ndarray], key: str) -> dict[str, dict[str, np.ndarray]]:
    """Split a columnar result (already ORDER BY key) into {key value: {column: ndarray}}."""
    keys = cols[key]
    if len(keys) == 0:
        return {}
    bounds = np.flatnonzero(keys[1:] != keys[:-1]) + 1
    starts = np.concatenate(([0], bounds))
    ends = np.concatenate((bounds, [len(keys)]))
    return {keys[s]: {c: a[s:e] for c, a in cols.items() if c != key} for s, e in zip(starts, ends)}


def insert_arrow(cur: Any, table: str, data: dict[str, list], mode: str = "INSERT") -> int:
    """Bulk insert columns via a registered Arrow table. mode: 'INSERT' or 'INSERT OR REPLACE'."""
    tbl = pa.table(data)
    if tbl.num_rows == 0:
        return 0
    name = f"_arrow_{table}"
    cur.register(name, tbl)
    try:
        cols = ", ".join(f'"{c}"' for c in tbl.column_names)
        cur.execute(f"{mode} INTO {table} ({cols}) SELECT {cols} FROM {name}")
    finally:
        cur.unregister(name)
    return tbl.num_rows


def to_columns(objs: Iterable[Any], fields: Sequence[str]) -> dict[str, list]:
    """Dataclass instances -> {field: [values]}."""
    objs = list(objs)
    return {f: [getattr(o, f) for o in objs] for f in fields}


def dumps(v: Any) -> str:
    """JSON-encode (dates -> ISO strings)."""
    return json.dumps(v, default=lambda o: o.isoformat() if isinstance(o, (date, datetime)) else str(o))


def loads(v: Any) -> Any:
    """Decode a JSON column value (None passes through)."""
    return None if v is None else json.loads(v)
