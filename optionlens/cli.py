"""Command line: `uv run python -m optionlens.cli <cmd>`.

Every data command runs through the scheduler's run_job (recorded in job_runs). DuckDB allows one
process per database file, so stop the server before running data commands against its DATA_DIR.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date

from . import db, net
from .jobs import scheduler
from .jobs.calendars import earnings_backfill_progress
from .jobs.universe import ensure_symbol
from .models import normalize_symbol
from .repo import jobs as jobs_repo
from .repo import status as status_repo
from .repo import symbols as symbols_repo


def _symbols(v: str | None) -> list[str] | None:
    return [normalize_symbol(s) for s in v.split(",") if s.strip()] if v else None


async def _run(name: str, **kwargs) -> int:
    try:
        ok, detail = await scheduler.run_job(name, **{k: v for k, v in kwargs.items() if v is not None})
    finally:
        await net.aclose()
    print(f"{name}: {'ok' if ok else 'FAILED'} {detail or ''}")
    return 0 if ok else 1


async def _ensure_symbols(symbols: list[str] | None) -> None:
    """Create symbols rows for explicitly requested symbols unknown to the database."""
    for s in symbols or []:
        await ensure_symbol(s)


def _status() -> int:
    counts = status_repo.row_counts()
    print(f"db: {db.path()} ({status_repo.db_size_bytes() / 1e6:.1f} MB)")
    for t, n in counts.items():
        print(f"  {t:18s} {n:>12,d}")
    print(f"symbols: {symbols_repo.counts()}")
    print(f"metrics_daily dates: {status_repo.distinct_dates('metrics_daily')}, "
          f"option_eod dates: {status_repo.distinct_dates('option_eod')}")
    print(f"earnings backfill: {earnings_backfill_progress():.0%}")
    for job, r in sorted(jobs_repo.latest_per_job().items()):
        print(f"  {job:18s} {r['started_at']:%Y-%m-%d %H:%M} ok={r['ok']} {(r['detail'] or '')[:100]}")
    return 0


def _serve() -> int:
    """uvicorn on HOST:PORT, one worker (DuckDB single process; the app lifespan opens the DB)."""
    import uvicorn

    from .api.app import create_app
    from .config import get_settings

    s = get_settings()
    uvicorn.run(create_app(), host=s.host, port=s.port, workers=1, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="optionlens.cli")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("universe")
    b = sub.add_parser("backfill-prices")
    b.add_argument("--symbols")
    b.add_argument("--limit", type=int)
    sub.add_parser("refresh-prices").add_argument("--symbols")
    e = sub.add_parser("earnings")
    e.add_argument("--backfill", action="store_true")
    e.add_argument("--from", dest="start", type=date.fromisoformat)
    e.add_argument("--to", dest="end", type=date.fromisoformat)
    d = sub.add_parser("dividends")
    d.add_argument("--history", action="store_true", help="per-symbol histories instead of the calendar")
    d.add_argument("--symbols")
    sub.add_parser("vix")
    sub.add_parser("rates")
    sub.add_parser("sweep").add_argument("--symbols")
    sub.add_parser("eod").add_argument("--symbols")
    sub.add_parser("prune")
    sub.add_parser("status")
    sub.add_parser("serve", help="run the API + scheduler (uvicorn, single worker)")
    r = sub.add_parser("run-job")
    r.add_argument("name", choices=sorted(scheduler.JOBS))
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "serve":
        return _serve()
    db.connect()
    try:
        match a.cmd:
            case "init-db":
                print(f"database ready: {db.path()}")
                return 0
            case "status":
                return _status()
            case "universe":
                return asyncio.run(_run("universe"))
            case "backfill-prices":
                syms = _symbols(a.symbols)
                async def go():
                    await _ensure_symbols(syms)
                    return await _run("prices_backfill", symbols=syms, limit=a.limit)
                return asyncio.run(go())
            case "refresh-prices":
                return asyncio.run(_run("prices_refresh", symbols=_symbols(a.symbols)))
            case "earnings":
                if a.backfill:
                    return asyncio.run(_run("earnings_backfill"))
                return asyncio.run(_run("earnings", start=a.start, end=a.end))
            case "dividends":
                if a.history:
                    return asyncio.run(_run("dividends_history", symbols=_symbols(a.symbols)))
                return asyncio.run(_run("dividends"))
            case "vix" | "rates" | "prune":
                return asyncio.run(_run(a.cmd))
            case "sweep" | "eod":
                syms = _symbols(a.symbols)
                async def go():
                    await _ensure_symbols(syms)
                    return await _run(a.cmd, symbols=syms)
                return asyncio.run(go())
            case "run-job":
                return asyncio.run(_run(a.name))
    finally:
        db.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
