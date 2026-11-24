"""Report contract types (ReportDef / ParamDef / ColumnDef of web/src/api/types.ts), query-param
parsing per ParamDef type, and JSON-safe row conversion. No I/O.

A report is ONE authoritative ``ReportDef`` (id, title, category, description, params, columns,
default_sort, static notes) plus its ``run`` function. ``run(p: RunParams)`` returns an ``Output``
(or a plain list of row dicts); it may be a coroutine function.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

import numpy as np

from ...analytics.calendar import now_ny
from ...models import normalize_symbol
from ..errors import BadRequest

FMTS = frozenset({"symbol", "text", "int", "num", "num2", "money", "bigmoney", "pct", "pct_signed", "vol",
                  "date", "datetime", "bool", "osi", "ratio", "badge", "halt"})
CATEGORIES = ("Options", "Strategies", "Earnings", "Volatility", "Stocks", "Dividends")
PARAM_TYPES = frozenset({"int", "number", "text", "select", "bool", "date", "symbols", "watchlist"})
MAX_ROWS = 5000


class ParamError(BadRequest):
    """Invalid query parameter (-> HTTP 422)."""


# ----------------------------------------------------------------------------- columns
@dataclass(frozen=True)
class Col:
    key: str
    label: str
    fmt: str
    group: str | None = None
    description: str | None = None
    default_hidden: bool = False

    def __post_init__(self):
        if self.fmt not in FMTS:
            raise ValueError(f"column {self.key}: bad fmt {self.fmt}")

    def as_dict(self) -> dict:
        d: dict = {"key": self.key, "label": self.label, "fmt": self.fmt}
        if self.group:
            d["group"] = self.group
        if self.description:
            d["description"] = self.description
        if self.default_hidden:
            d["default_hidden"] = True
        return d

    def grouped(self, group: str | None) -> "Col":
        return Col(self.key, self.label, self.fmt, group, self.description, self.default_hidden)

    def hidden(self, flag: bool = True) -> "Col":
        return Col(self.key, self.label, self.fmt, self.group, self.description, flag)


def group(name: str | None, cols: Iterable[Col]) -> tuple[Col, ...]:
    """Same columns assigned to a column group."""
    return tuple(c.grouped(name) for c in cols)


def pick(cols: Iterable[Col], *keys: str) -> tuple[Col, ...]:
    """Columns by key, in the given key order (KeyError for unknown keys)."""
    by = {c.key: c for c in cols}
    return tuple(by[k] for k in keys)


# ----------------------------------------------------------------------------- params
_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def today() -> date:
    return now_ny().date()


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    type: str
    default: Any = None                       # value, or a zero-arg callable (e.g. relative dates)
    options: tuple[tuple[str, str], ...] | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str | None = None

    def __post_init__(self):
        if self.type not in PARAM_TYPES:
            raise ValueError(f"param {self.key}: bad type {self.type}")
        if self.type == "select" and not self.options:
            raise ValueError(f"param {self.key}: select needs options")

    def default_value(self) -> Any:
        return self.default() if callable(self.default) else self.default

    def as_dict(self) -> dict:
        d: dict = {"key": self.key, "label": self.label, "type": self.type, "default": _jsonable(self.default_value())}
        if self.options:
            d["options"] = [{"value": v, "label": l} for v, l in self.options]
        for k in ("min", "max", "step", "help"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    def parse(self, raw: str | None) -> Any:
        """Query-string value -> typed value (default when missing/empty). ParamError when invalid."""
        if raw is None or raw.strip() == "":
            return self.default_value()
        s = raw.strip()
        t = self.type
        try:
            if t == "int":
                f = float(s)
                if not f.is_integer():
                    raise ValueError
                v: Any = int(f)
            elif t == "number":
                v = float(s)
                if not math.isfinite(v):
                    raise ValueError
            elif t == "bool":
                if s.lower() in _TRUE:
                    return True
                if s.lower() in _FALSE:
                    return False
                raise ValueError
            elif t == "date":
                return date.fromisoformat(s)
            elif t == "select":
                if s not in {o[0] for o in self.options or ()}:
                    raise ValueError
                return s
            elif t == "symbols":
                syms = [normalize_symbol(x) for x in s.replace(";", ",").replace(" ", ",").split(",") if x.strip()]
                return list(dict.fromkeys(syms)) or None
            elif t == "watchlist":
                return int(s)
            else:
                return s
        except ValueError:
            raise ParamError(f"invalid value for {self.key}: {raw!r}") from None
        if self.min is not None and v < self.min:
            raise ParamError(f"{self.key} must be >= {self.min}")
        if self.max is not None and v > self.max:
            raise ParamError(f"{self.key} must be <= {self.max}")
        return v


def rel_date(days: int) -> Callable[[], date]:
    """Default factory: today + days (New York)."""
    return lambda: today() + timedelta(days=days)


class RunParams(dict):
    """Effective params (JSON-able) plus ``scope``: the symbol restriction from `symbols` and/or
    `watchlist` (intersection when both are given), None when unrestricted."""
    scope: list[str] | None = None


# ----------------------------------------------------------------------------- output
@dataclass
class Output:
    rows: list[dict]
    notes: list[str] = field(default_factory=list)
    extra: dict | None = None
    asof: datetime | None = None
    truncated: bool = False
    presorted: bool = False          # rows already in the intended order (skip default_sort)
    after_hours: bool = False        # some rows' session values are an after-hours fill (metrics_daily.after_hours)


@dataclass(frozen=True)
class ReportDef:
    id: str
    title: str
    category: str
    description: str
    params: tuple[Param, ...]
    columns: tuple[Col, ...]
    run: Callable[[RunParams], Any]
    default_sort: tuple[str, str] | None = None     # (key, "asc"|"desc")
    notes: tuple[str, ...] = ()                       # static limitations / proxies, in plain words
    method: tuple[str, ...] = ()                      # the precise method behind the notes (shown on request)
    empty_reason: str | None = None                   # why a run can return no rows (when no run-time note says)

    def as_dict(self) -> dict:
        d = {"id": self.id, "title": self.title, "category": self.category, "description": self.description,
             "params": [p.as_dict() for p in self.params], "columns": [c.as_dict() for c in self.columns]}
        if self.default_sort:
            d["default_sort"] = {"key": self.default_sort[0], "dir": self.default_sort[1]}
        return d

    def column_keys(self) -> list[str]:
        return [c.key for c in self.columns]


# ----------------------------------------------------------------------------- JSON conversion
def _jsonable(v: Any) -> Any:
    """numpy scalars -> Python, NaN/inf -> None, date/datetime -> ISO strings (recursively)."""
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, int):
        return v
    if isinstance(v, np.generic):
        return _jsonable(v.item())
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, np.datetime64):
        return None if np.isnat(v) else _jsonable(v.astype("datetime64[D]").item())
    if isinstance(v, Mapping):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_jsonable(x) for x in v]
    return v


jsonable = _jsonable


def sort_rows(rows: list[dict], key: str, descending: bool) -> list[dict]:
    """Stable sort by key; None/NaN always last."""
    present = [r for r in rows if not _missing(r.get(key))]
    absent = [r for r in rows if _missing(r.get(key))]
    present.sort(key=lambda r: r[key], reverse=descending)
    return present + absent


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and not math.isfinite(v))


def fnum(v) -> float | None:
    """float or None (NaN/inf -> None)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def ratio(a, b) -> float | None:
    a, b = fnum(a), fnum(b)
    if a is None or not b:
        return None
    return a / b
