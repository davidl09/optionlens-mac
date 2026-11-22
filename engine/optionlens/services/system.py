"""Health, status page data (incl. data-source and IBKR-bridge health) and symbol search."""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from importlib import metadata

from ..analytics.calendar import is_market_open, now_ny, prev_trading_day
from ..config import Settings, get_settings
from ..jobs import scheduler
from ..jobs.calendars import earnings_backfill_progress
from ..repo import bridge as bridge_repo
from ..repo import jobs as jobs_repo
from ..repo import options as options_repo
from ..repo import status as status_repo
from ..repo import symbols as symbols_repo

SEARCH_LIMIT = 20
JOB_FIELDS = ("job", "started_at", "finished_at", "ok", "detail")
# Status.sources: each server-side data source and the scheduler jobs that collect from it ("vendor":
# the optional server-side ChainSource, listed only when OPTIONLENS_CHAIN_SOURCE is configured).
SOURCE_JOBS = {
    "vendor": ("sweep", "eod"),
    "nasdaq": ("universe", "prices_backfill", "prices_refresh", "earnings", "earnings_backfill", "dividends",
               "dividends_history"),
    "treasury": ("rates",),
}
COVERAGE_SESSIONS = 5   # optionable_count: universe symbols with a chain stored within this many sessions
BRIDGE_STALE_S = 600   # no heartbeat for this long -> bridge not connected (it polls /wants every 5 min)


_JSON_TAIL = re.compile(r"\{[^{}]*\}\s*$")
_FLOAT = re.compile(r"(?<![\w.])-?\d+\.\d{5,}(?![\w.])")


def human_detail(detail: str | None) -> str | None:
    """A job's detail for people: a trailing JSON progress object (kept raw in job_runs as the resume state) as
    'key value' pairs, counts with thousands separators, float noise (0.042300000000000004) rounded."""
    if not detail:
        return detail
    m = _JSON_TAIL.search(detail)
    if m:
        try:
            obj = json.loads(m.group(0))
            pairs = ", ".join(f"{k} {f'{v:,}' if isinstance(v, int) else v}" for k, v in obj.items())
            detail = (detail[:m.start()].rstrip() + " " if detail[:m.start()].strip() else "") + pairs
        except ValueError:
            pass
    return _FLOAT.sub(lambda f: f"{float(f.group(0)):.6g}", detail)


def _job(r: dict) -> dict:
    return {**{k: r[k] for k in JOB_FIELDS}, "detail": human_detail(r["detail"])}


@lru_cache(maxsize=1)
def version() -> str:
    try:
        return metadata.version("optionlens")
    except metadata.PackageNotFoundError:
        return "0.0.0"


@lru_cache(maxsize=4)
def _server_id(data_dir) -> str:
    path = data_dir / "server_id"
    if not path.is_file():
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(16) + "\n")
    return path.read_text().strip()


def server_id() -> str:
    """Stable random id of this server's DATA_DIR (created on first use)."""
    return _server_id(get_settings().data_dir)


def health(s: Settings) -> dict:
    """GET /api/health for the app serving with settings `s`."""
    return {"ok": True, "version": version(), "auth": s.auth_enabled, "server_id": server_id()}


def _source(name: str, latest: dict[str, dict]) -> dict:
    """{name, ok, last_success_at, detail} from the latest finished run of each of the source's jobs."""
    runs = [latest[j] for j in SOURCE_JOBS[name] if j in latest and latest[j]["finished_at"] is not None]
    failed = [r for r in runs if not r["ok"]]
    oks = [r["finished_at"] for j in SOURCE_JOBS[name] if (r := jobs_repo.latest_run(j, ok=True))]
    return {"name": name, "ok": (not failed) if runs else None, "last_success_at": max(oks, default=None),
            "detail": "; ".join(f"{r['job']}: {r['detail'] or 'failed'}" for r in failed) or None}


def bridge(now: datetime | None = None) -> dict:
    """Status.bridge: connected = a heartbeat within BRIDGE_STALE_S reporting IBKR connected."""
    b = bridge_repo.get_status() or {}
    beat = b.get("heartbeat_at")
    fresh = beat is not None and (now or datetime.now(timezone.utc)) - beat <= timedelta(seconds=BRIDGE_STALE_S)
    connected = bool(fresh and b.get("ibkr_connected"))
    return {"connected": connected, "last_push_at": b.get("last_push_at"), "client": b.get("client"),
            "farms": b.get("farms") if connected else None, "options_subscription": b.get("options_subscription")}


def sources(latest: dict[str, dict], br: dict) -> list[dict]:
    """nasdaq, treasury, ibkr-bridge (heartbeat / last push) and vendor (only when configured)."""
    ibkr = {"name": "ibkr-bridge", "ok": br["connected"] if br["client"] else None,
            "last_success_at": br["last_push_at"], "detail": br["client"]}
    vendor = [_source("vendor", latest)] if get_settings().chain_source != "none" else []
    return [_source("nasdaq", latest), _source("treasury", latest), ibkr, *vendor]


def _sessions_back(d, n: int):
    for _ in range(n):
        d = prev_trading_day(d)
    return d


def status() -> dict:
    now = now_ny()
    counts = symbols_repo.counts()
    sweep = jobs_repo.latest_run("sweep", ok=True)
    per_job = jobs_repo.latest_per_job()
    latest = sorted(per_job.values(), key=lambda r: r["job"])
    br = bridge()
    seen = {r["id"] for r in latest}
    recent = [r for r in jobs_repo.recent_runs(50) if r["id"] not in seen]
    return {
        "now": now, "market_open": is_market_open(now), "next_sweep_at": scheduler.next_run_at("sweep", now),
        "universe_size": counts["universe_size"],
        "optionable_count": options_repo.chain_coverage(_sessions_back(now.date(), COVERAGE_SESSIONS),
                                                        symbols_repo.universe_symbols(include_unoptionable=True)),
        "options_listed_count": counts["optionable_count"], "no_options_count": counts["no_options_count"],
        "last_sweep_at": sweep["finished_at"] if sweep else None,
        "last_eod_date": options_repo.prior_eod_date(now.date() + timedelta(days=1)),
        "iv_history_days": status_repo.distinct_dates("metrics_daily"),
        "option_eod_days": status_repo.distinct_dates("option_eod"),
        "earnings_backfill_progress": earnings_backfill_progress(),
        "row_counts": status_repo.row_counts(),
        "jobs": [_job(r) for r in latest + recent],
        "db_size_bytes": status_repo.db_size_bytes(),
        "sources": sources(per_job, br), "bridge": br,
    }


def search(q: str) -> list[dict]:
    return symbols_repo.search_symbols(q, SEARCH_LIMIT)
