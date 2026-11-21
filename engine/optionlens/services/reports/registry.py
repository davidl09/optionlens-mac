"""The report registry: every ReportDef, registered once, plus `run_report` (param parsing, scope
resolution, execution, server-side sort, truncation, JSON-safe ReportResult)."""
from __future__ import annotations

import inspect
from typing import Mapping

import anyio.to_thread

from ...repo import user as user_repo
from ..errors import NotFound
from . import calendars, dividends, earnings, options, stocks, strategies, volatility
from .columns import NOTE_CHAIN_UNIVERSE
from .base import MAX_ROWS, Output, ParamError, ReportDef, RunParams, jsonable, sort_rows

NO_CHAIN_MATCH = "No stored option chain matches these settings."

REPORTS: dict[str, ReportDef] = {}
for _d in (*options.REPORTS, *strategies.REPORTS, *earnings.REPORTS, *volatility.REPORTS, *stocks.REPORTS,
           *dividends.REPORTS, *calendars.REPORTS):
    if _d.id in REPORTS:
        raise RuntimeError(f"duplicate report id {_d.id}")
    REPORTS[_d.id] = _d


def definitions() -> list[dict]:
    """ReportDef[] (types.ts) for GET /api/reports."""
    return [d.as_dict() for d in REPORTS.values()]


def get(report_id: str) -> ReportDef:
    d = REPORTS.get(report_id)
    if d is None:
        raise NotFound(f"unknown report {report_id!r}")
    return d


def parse_params(d: ReportDef, query: Mapping[str, str]) -> RunParams:
    """Typed effective params from query strings (defaults for missing); resolves `symbols` and
    `watchlist` into RunParams.scope. ParamError for invalid values / unknown watchlist."""
    p = RunParams({prm.key: prm.parse(query.get(prm.key)) for prm in d.params})
    scope = None
    if p.get("symbols"):
        scope = list(p["symbols"])
    if p.get("watchlist") is not None:
        wl = user_repo.get_watchlist(int(p["watchlist"]))
        if wl is None:
            raise ParamError(f"unknown watchlist {p['watchlist']}")
        scope = [s for s in (scope or wl["symbols"]) if s in set(wl["symbols"])]
    p.scope = scope
    return p


async def run_report(report_id: str, query: Mapping[str, str]) -> dict:
    """ReportResult for a report id and raw query params (NotFound / ParamError)."""
    d = get(report_id)
    p = parse_params(d, query)
    return finalize(d, p, await execute(d, p))


async def execute(d: ReportDef, p: RunParams) -> Output:
    if inspect.iscoroutinefunction(d.run):
        out = await d.run(p)
    else:
        out = await anyio.to_thread.run_sync(d.run, p)
    return out if isinstance(out, Output) else Output(list(out))


def run_sync(report_id: str, query: Mapping[str, str] | None = None) -> dict:
    """Synchronous variant for jobs/alerts/tests (sync run functions only)."""
    d = get(report_id)
    p = parse_params(d, query or {})
    out = d.run(p)
    if inspect.isawaitable(out):
        raise TypeError(f"{report_id} is async; use run_report")
    return finalize(d, p, out if isinstance(out, Output) else Output(list(out)))


def finalize(d: ReportDef, p: RunParams, out: Output) -> dict:
    rows = out.rows
    if d.default_sort and not out.presorted:
        rows = sort_rows(rows, d.default_sort[0], d.default_sort[1] == "desc")
    truncated = out.truncated or len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return {
        "id": d.id, "title": d.title, "asof": jsonable(out.asof), "params": jsonable(dict(p)),
        "columns": [c.as_dict() for c in d.columns], "rows": jsonable(rows), "truncated": truncated,
        "notes": [*d.notes, *out.notes], **({"extra": jsonable(out.extra)} if out.extra is not None else {}),
        **({"method": list(d.method)} if d.method else {}),
        **({"empty_reason": reason} if not rows and (reason := empty_reason(d, out)) else {}),
    }


def empty_reason(d: ReportDef, out: Output) -> str | None:
    """Why a run returned no rows: its run-time notes (they explain the empty result), else the report's own
    `empty_reason`, else for chain-based reports that no stored chain matched."""
    run_time = " ".join(n for n in out.notes if n not in d.notes)
    if run_time:
        return run_time
    if d.empty_reason:
        return d.empty_reason
    return NO_CHAIN_MATCH if NOTE_CHAIN_UNIVERSE in d.notes else None


def compare_straddle(symbols: list[str], dte: int = 30) -> dict:
    """ReportResult for /api/compare/straddle (sync: refreshes each symbol's chain on demand)."""
    d = volatility.COMPARE_STRADDLE
    p = parse_params(d, {"symbols": ",".join(symbols), "dte": str(dte)})
    out = d.run(p)
    return finalize(d, p, out)
