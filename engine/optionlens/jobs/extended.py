"""Extended-hours quotes of the whole universe (docs/INTEGRATION.md §8 "Extended hours"): while a trading day's
pre-market (04:00-09:30 ET) or after-hours (close-20:00 ET) runs, every COLLECT_EVERY_MIN minutes each available
server-side source that can (Questrade: equity quotes, 100 ids per call, ids cached by the sweeps) is asked for the
universe and watchlist symbols; the latest quote per symbol and market is stored (repo.extended), halt flags too
(repo.halts; a change fires jobs.hooks.on_halt_change). The pre-market / after-hours movers reports read them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from ..analytics import intraday
from ..analytics.calendar import NY, now_ny
from ..models import ExtendedQuote
from ..repo import extended as extended_repo
from ..repo import halts as halts_repo
from ..repo import symbols as symbols_repo
from ..repo import user as user_repo
from ..sources import chain_source
from . import hooks

COLLECT_EVERY_MIN = 5


class ExtendedSource(Protocol):
    name: str

    def unavailable(self) -> str | None: ...

    async def fetch_extended(self, symbols: list[str]) -> list[ExtendedQuote]: ...


def _sources() -> list[ExtendedSource]:
    return [s for s in chain_source.server_sources() if hasattr(s, "fetch_extended")]


def _symbols() -> list[str]:
    return list(dict.fromkeys([*user_repo.watchlist_symbols(), *symbols_repo.universe_symbols(include_unoptionable=True)]))


def store(market: str, now: datetime, quotes: list[ExtendedQuote], source: str) -> tuple[int, list[str]]:
    """Store one collection: (rows written, symbols whose halt state changed)."""
    n = extended_repo.upsert(market, now.date(), quotes, source)
    changed = halts_repo.record([(q.symbol, q.halted, q.ts) for q in quotes if q.halted is not None], source)
    return n, changed


async def collect(now: datetime | None = None) -> str:
    """One collection round (see module doc); skipped outside the extended-hours windows."""
    now = (now or now_ny()).astimezone(NY)
    market = intraday.extended_market(now)
    if market is None:
        return "skipped: not in pre-market (04:00-09:30 ET) or after-hours (close-20:00 ET) of a trading day"
    syms = await asyncio.to_thread(_symbols)
    details = []
    for src in _sources():
        why = src.unavailable()
        if why is not None:
            details.append(f"{src.name}: {why}")
            continue
        quotes = await src.fetch_extended(syms)
        n, changed = await asyncio.to_thread(store, market, now, quotes, src.name)
        if changed:
            await hooks.fire(hooks.on_halt_change, now.date(), changed)
        details.append(f"{src.name}: {n} {market}-market quotes of {len(syms)} symbols")
    return "; ".join(details) or "no source offers extended-hours quotes"
