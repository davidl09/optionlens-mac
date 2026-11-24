"""Context-friendly tool output (docs/INTEGRATION.md §10): compact JSON, long arrays paginated with a note of what was
cut, column subsets of table rows, the user's table state (filters, sort) applied to rows of the current view, and the
compact report catalog. Pure: no I/O. Numbers are passed through unchanged (the API's units)."""
from __future__ import annotations

import functools
import json
import math
import re
from typing import Any, Iterable, Mapping

from .catalog import PAGE_DEFAULT, PAGE_MAX


def dumps(value: Any) -> str:
    """Compact JSON (no spaces, UTF-8 kept, NaN/inf never written)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ----------------------------------------------------------------------------- paging and columns
def page_args(args: Mapping[str, Any]) -> tuple[int, int, list[str] | None]:
    """(limit, offset, columns) of a tool call, bounded (limit 1..PAGE_MAX, default PAGE_DEFAULT)."""
    limit = args.get("limit")
    limit = PAGE_DEFAULT if not isinstance(limit, int) or isinstance(limit, bool) else max(1, min(PAGE_MAX, limit))
    offset = args.get("offset")
    offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
    cols = args.get("columns")
    cols = [str(c) for c in cols] if isinstance(cols, list) and cols else None
    return limit, offset, cols


def paginate(value: Any, offset: int = 0, limit: int = PAGE_DEFAULT) -> Any:
    """Every array longer than `limit` (at any depth) is cut to [offset, offset + limit) (offset < 0: from the end);
    a top-level `_page` {json.path: {total, offset, returned}} says what was cut (arrays inside arrays: the largest).
    A cut top-level array is returned as {"items": [...], "_page": ...}."""
    pages: dict[str, dict] = {}

    def walk(v: Any, path: str) -> Any:
        if isinstance(v, list):
            n = len(v)
            if n > limit:
                start = max(0, n + offset) if offset < 0 else min(offset, n)
                v = v[start:start + limit]
                seen = pages.get(path or "items")
                if seen is None or n > seen["total"]:
                    pages[path or "items"] = {"total": n, "offset": start, "returned": len(v)}
            return [walk(x, f"{path}[]") for x in v]
        if isinstance(v, dict):
            return {k: walk(x, f"{path}.{k}" if path else k) for k, x in v.items()}
        return v

    out = walk(value, "")
    if not pages:
        return out
    if isinstance(out, list):
        return {"items": out, "_page": pages}
    return {**out, "_page": pages} if isinstance(out, dict) else out


def select_columns(value: Any, columns: Iterable[str]) -> Any:
    """Table rows (arrays of objects holding any of `columns`) keep only those keys; a ReportResult's `columns`
    definitions are narrowed the same way."""
    keep = list(dict.fromkeys(columns))

    def walk(v: Any) -> Any:
        if isinstance(v, list):
            if v and all(isinstance(x, dict) for x in v) and any(k in x for x in v for k in keep):
                return [{k: x[k] for k in keep if k in x} for x in v]
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: ([c for c in x if isinstance(c, dict) and c.get("key") in keep]
                        if k == "columns" and isinstance(x, list) and isinstance(v.get("rows"), list) else walk(x))
                    for k, x in v.items()}
        return v

    return walk(value)


def shape(value: Any, args: Mapping[str, Any]) -> Any:
    """A read tool's answer for an agent: columns selected, then long arrays paginated."""
    limit, offset, cols = page_args(args)
    if cols:
        value = select_columns(value, cols)
    return paginate(value, offset, limit)


# ----------------------------------------------------------------------------- the user's table state
# The same rule as the renderer's tables (src/lib/table.ts applyTable); engine/tests/fixtures/table-state-cases.json
# is run by both test suites.
def _missing(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, float) and not math.isfinite(v))


def js_string(v: Any) -> str:
    """JavaScript's String(v) for JSON values (true, 5, 0.25)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    return str(v)


def _js_number(v: Any) -> float:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return math.nan


def _active(f: Mapping) -> bool:
    return (f.get("min") is not None or f.get("max") is not None or bool(f.get("text"))
            or bool(f.get("values")))


def _passes(v: Any, f: Mapping) -> bool:
    lo, hi = f.get("min"), f.get("max")
    if lo is not None or hi is not None:
        n = _js_number(v)
        if _missing(v) or not math.isfinite(n):
            return False
        if lo is not None and n < lo:
            return False
        if hi is not None and n > hi:
            return False
    if f.get("text"):
        if _missing(v) or str(f["text"]).lower() not in js_string(v).lower():
            return False
    if f.get("values"):
        if ("" if _missing(v) else js_string(v)) not in f["values"]:
            return False
    return True


_CHUNK = re.compile(r"(\d+)")


def _natural(s: str) -> list:
    return [(0, int(c), "") if c.isdigit() else (1, 0, c.casefold()) for c in _CHUNK.split(s) if c != ""]


def _locale_compare(a: str, b: str) -> int:
    """String.localeCompare(b, undefined, {numeric: true}), approximately: digit runs as numbers, case-insensitive,
    then lower case first."""
    ka, kb = _natural(a), _natural(b)
    if ka != kb:
        return -1 if ka < kb else 1
    sa, sb = a.swapcase(), b.swapcase()
    return (sa > sb) - (sa < sb)


def _compare(a: Any, b: Any) -> float:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        return a - b
    if isinstance(a, bool) and isinstance(b, bool):
        return int(a) - int(b)
    return _locale_compare(js_string(a), js_string(b))


def apply_table(rows: list[dict], table: Mapping[str, Any] | None) -> list[dict]:
    """Rows after a table state {sort: {key, dir}, filters: {key: {min, max, text, values}}, symbols?}: the filtered
    rows (`symbols`: only rows of these symbols, the "Watchlist only" filter), sorted with missing values last."""
    if not table:
        return list(rows)
    active = [(k, f) for k, f in (table.get("filters") or {}).items() if isinstance(f, Mapping) and _active(f)]
    out = [r for r in rows if all(_passes(r.get(k), f) for k, f in active)]
    if table.get("symbols") is not None:
        keep = set(table["symbols"])
        out = [r for r in out if isinstance(r.get("symbol"), str) and r["symbol"] in keep]
    s = table.get("sort")
    if s and s.get("key"):
        key, sign = s["key"], 1 if s.get("dir") == "asc" else -1

        def cmp(ra: dict, rb: dict) -> int:
            a, b = ra.get(key), rb.get(key)
            ma, mb = _missing(a), _missing(b)
            if ma or mb:
                return 0 if ma and mb else (1 if ma else -1)
            c = sign * _compare(a, b)
            return (c > 0) - (c < 0)

        out = sorted(out, key=functools.cmp_to_key(cmp))
    return out


# ----------------------------------------------------------------------------- the report catalog
def report_list(defs: list[dict], args: Mapping[str, Any]) -> Any:
    """report_list: compact entries (id, title, category, description, param keys), or one report's full definition
    (`report_id`). KeyError for an unknown report id."""
    rid = args.get("report_id")
    if rid:
        for d in defs:
            if d.get("id") == rid:
                return d
        raise KeyError(f"unknown report {rid!r}: see report_list for the ids")
    cat = args.get("category")
    return [{"id": d["id"], "title": d["title"], "category": d["category"], "description": d["description"],
             "params": [p["key"] for p in d.get("params", [])]}
            for d in defs if not cat or d.get("category") == cat]
