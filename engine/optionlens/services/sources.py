"""Server-side data sources the user connects in Settings → Data sources (docs/INTEGRATION.md §8): Questrade's
connection (paste / forget a refresh token) and its status. Tokens are never returned or logged."""
from __future__ import annotations

import asyncio

from ..analytics.calendar import now_ny
from ..jobs import scheduler, source_health
from ..jobs._util import after_session
from ..repo import jobs as jobs_repo
from ..repo import options as options_repo
from ..sources import chain_source, questrade
from ..sources.questrade import QuestradeSource
from .errors import BadRequest, NotFound

SWEEP_JOBS = ("sweep", "sweep_final", "sweep_fill")
UPSTREAM = "upstream"   # BadRequest code: Questrade's login could not be reached (retry later)


def _questrade() -> QuestradeSource:
    if questrade.NAME not in chain_source.server_source_names():
        raise NotFound("Questrade is not enabled (OPTIONLENS_CHAIN_SOURCES does not list it)")
    return chain_source.REGISTRY[questrade.NAME]()   # type: ignore[return-value]


def _last_sweep(name: str) -> tuple[object, str | None]:
    """(finished_at, this source's part of the detail) of the latest finished sweep-family run."""
    runs = [r for j in SWEEP_JOBS if (r := jobs_repo.latest_run(j)) and r["finished_at"] is not None]
    if not runs:
        return None, None
    last = max(runs, key=lambda r: r["finished_at"])
    part = next((p.strip() for p in (last["detail"] or "").split(";") if name in p), last["detail"])
    return last["finished_at"], part


def questrade_status() -> dict:
    """GET /api/sources/questrade: state (disconnected | connected | expired | error | needs_attention), detail,
    last refresh, last chain served, last sweep, the top failure reasons of the last 24 h ([{reason, count}]), call
    pacing (calls in the last hour of the hourly budget, the API's remaining / reset), whether the last chain was
    delayed, the last token check (verification), the sweep shape."""
    src = _questrade()
    at, detail = _last_sweep(src.name)
    return {"name": src.name, **src.status(), "last_chain_at": options_repo.last_fetched_by(src.name),
            "last_sweep_at": at, "last_sweep_detail": detail, "failures": source_health.top_failures(src.name)}


def _summary(st: dict) -> str:
    """One line for Status → Data sources: state, data type, calls this hour, last sweep."""
    parts = [st["detail"] or {"disconnected": "not connected (Settings → Data sources)"}.get(st["state"], st["state"])]
    if st["delayed"] is not None:
        parts.append("delayed data" if st["delayed"] else "real-time data")
    if st["failures"]:
        parts.append(f"failures (24 h): {source_health.format_failures(st['failures'])}")
    parts.append(f"{st['calls_last_hour']:,}/{st['hour_budget']:,} calls this hour"
                 + (f" (Questrade reports {st['remaining']:,} left)" if st["remaining"] is not None else ""))
    if st["last_sweep_detail"]:
        parts.append(f"last sweep: {st['last_sweep_detail']}")
    return " · ".join(parts)


def status_entry() -> list[dict]:
    """Status.sources entries ({name, ok, last_success_at, detail}) of the enabled server-side sources."""
    if questrade.NAME not in chain_source.server_source_names():
        return []
    st = questrade_status()
    ok = {"connected": not source_health.failing(st["name"]), "disconnected": None}.get(st["state"], False)
    return [{"name": st["name"], "ok": ok, "last_success_at": st["last_chain_at"], "detail": _summary(st)}]


async def connect_questrade(refresh_token: str) -> dict:
    """POST /api/sources/questrade/token: redeem and store a pasted refresh token (clearing a needs_attention state
    and the earlier failures), check it with one market-data call (QuestradeSource.verify: "Token OK: market data
    permitted", or the classified error, e.g. the missing market-data permission), and when it passes start
    collecting at once (a sweep in the session, else the after-hours fill). 422 code "token_rejected" when Questrade
    refuses the token, "upstream" when its login cannot be reached."""
    src = _questrade()
    try:
        await src.tokens.connect(refresh_token)
    except questrade.TokenRejected as e:
        raise BadRequest(str(e), "token_rejected") from None
    except questrade.TokenError as e:
        raise BadRequest(str(e), UPSTREAM) from None
    source_health.reset(src.name)
    if (await src.verify())["ok"]:
        scheduler.run_soon("sweep_fill" if after_session(now_ny()) else "sweep")
    return await asyncio.to_thread(questrade_status)


def disconnect_questrade() -> dict:
    """DELETE /api/sources/questrade/token: forget the stored token."""
    src = _questrade()
    src.tokens.disconnect()
    src.verification = None
    source_health.reset(src.name)
    return questrade_status()
