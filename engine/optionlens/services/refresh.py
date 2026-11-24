"""On-demand chain refresh of one symbol from every connected chain source at once (docs/INTEGRATION.md §7).

`start` (the user opened the symbol or clicked Retry) begins a round: each connected server-side ChainSource
(Questrade) is fetched in the background here, and the IBKR bridge — fetched by the app itself, which asks it right
after starting the round — is marked pending while the bridge is connected; the bridge reports its outcome with
`report` (POST /api/ingest/refresh-result). Each chain is stored as it arrives (jobs.chains.store_chain), so the
first one is served at once and a better second one replaces it (jobs/precedence.py). `status` is the round's
one source-neutral view: pending / ok / no_options / unavailable / failed per source, and the served chain.

Rounds live in memory (they are about the last minutes only)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

from ..jobs import chains, ondemand
from ..models import normalize_symbol
from ..net import FetchError
from ..repo import options as options_repo
from ..sources import chain_source
from . import context as ctx
from . import system

log = logging.getLogger(__name__)

State = Literal["pending", "ok", "no_options", "unavailable", "failed"]
IBKR = "ibkr"
FRESH_S = 120               # a source that delivered a chain this recently is not asked again
BRIDGE_TIMEOUT_S = 300      # a pending IBKR refresh without a report after this long is reported as failed
LABELS = {IBKR: "IBKR", "questrade": "Questrade"}


@dataclass
class Attempt:
    state: State
    at: datetime
    message: str | None = None       # what happened / what to do, in full
    waiting_for: str | None = None   # IBKR "unavailable": "contract-server" | "market-hours"
    reason: str | None = None        # failed: the short form, e.g. "403 code 1016 (missing market-data permission)"


@dataclass
class Round:
    started_at: datetime
    attempts: dict[str, Attempt] = field(default_factory=dict)


_rounds: dict[str, Round] = {}
_tasks: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def label(source: str) -> str:
    return LABELS.get(source, source)


def _recent_ok(prev: Round | None, source: str, now: datetime) -> Attempt | None:
    a = prev.attempts.get(source) if prev else None
    return a if a is not None and a.state == "ok" and (now - a.at).total_seconds() < FRESH_S else None


def _pending(r: Round) -> bool:
    return any(a.state == "pending" for a in r.attempts.values())


async def start(symbol: str) -> dict:
    """Begin a refresh round of `symbol` (a round still pending is joined, not repeated). Raises UnknownSymbol."""
    sym = normalize_symbol(symbol)
    await ondemand.visit(sym)
    now = _now()
    prev = _rounds.get(sym)
    if prev is not None and _pending(prev) and (now - prev.started_at).total_seconds() < BRIDGE_TIMEOUT_S:
        return await asyncio.to_thread(status, sym)
    r = Round(now)
    if chain_source.bridge_serves(sym) and (await asyncio.to_thread(system.bridge))["connected"]:
        r.attempts[IBKR] = Attempt("pending", now)
    for src in chain_source.server_sources():
        if not src.connected():
            continue   # not set up: not part of the round
        if (ok := _recent_ok(prev, src.name, now)) is not None:
            r.attempts[src.name] = ok
            continue
        why = src.unavailable()
        if why is not None:   # set up but unusable (needs attention, token expired, login failing): say why
            r.attempts[src.name] = Attempt("failed", now, why, reason=src.problem())
            continue
        r.attempts[src.name] = Attempt("pending", now)
        task = asyncio.get_running_loop().create_task(_fetch(sym, src, r))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    _rounds[sym] = r
    return await asyncio.to_thread(status, sym)


async def _fetch(sym: str, src: chain_source.ChainSource, r: Round) -> None:
    try:
        snap = await chains.refresh_chain(src, sym)
        a = Attempt("ok" if snap is not None else "no_options", _now())
    except chain_source.NotListed:
        a = Attempt("unavailable", _now(), f"{label(src.name)} does not list {sym}")
    except FetchError as e:
        reason = chains.failure_reason(e)
        a = Attempt("failed", _now(), getattr(e, "attention", None) or reason, reason=reason)
    except Exception as e:  # noqa: BLE001 - reported, never raised into the loop
        log.exception("refresh %s from %s failed", sym, src.name)
        a = Attempt("failed", _now(), chains.failure_reason(e), reason=chains.failure_reason(e))
    r.attempts[src.name] = a
    if a.state == "ok":
        ctx.invalidate()


BRIDGE_STATES: dict[str, State] = {"pending": "pending", "refreshed": "ok", "fresh": "ok", "no-options": "no_options",
                                   "unavailable": "unavailable", "failed": "failed"}


def report(symbol: str, source: str, result: str, message: str | None = None, waiting_for: str | None = None) -> dict:
    """A client source's state for the symbol's current round: "pending" when the bridge accepted the refresh, then
    its outcome (RefreshResult status). A report without a current round starts one."""
    sym = normalize_symbol(symbol)
    now = _now()
    r = _rounds.get(sym)
    if r is None or (now - r.started_at).total_seconds() >= BRIDGE_TIMEOUT_S:
        r = _rounds[sym] = Round(now)
    r.attempts[source] = Attempt(BRIDGE_STATES[result], now, message, waiting_for)
    return status(sym)


def status(symbol: str) -> dict:
    """GET /api/symbol/{sym}/refresh: {symbol, started_at, pending, sources: [{name, state, message, waiting_for,
    reason, at}], served: {source, fetched_at, delayed} | null}. Without a round: no sources, pending false."""
    sym = normalize_symbol(symbol)
    now = _now()
    r = _rounds.get(sym)
    if r is not None:
        a = r.attempts.get(IBKR)
        if a is not None and a.state == "pending" and (now - a.at).total_seconds() >= BRIDGE_TIMEOUT_S:
            r.attempts[IBKR] = Attempt("failed", now, f"IBKR did not answer within {BRIDGE_TIMEOUT_S // 60} min")
    q = options_repo.latest_quality(sym)
    return {
        "symbol": sym, "started_at": r.started_at if r else None, "pending": bool(r and _pending(r)),
        "sources": [{"name": n, **asdict(a)} for n, a in sorted((r.attempts if r else {}).items())],
        "served": None if q is None else {"source": q["source"], "fetched_at": q["fetched_at"], "delayed": q["delayed"]},
    }
