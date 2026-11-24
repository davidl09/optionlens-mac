"""Live values -> the stored latest data, at a low rate (live/hub.py PERSIST_S): quotes_latest field-wise
(quotes_repo.merge_quotes), halt flags (repo.halts: a change fires jobs.hooks.on_halt_change, so `halted` / `resumed`
alerts follow live quotes) and the served option_latest rows' market fields (options_repo.patch_latest), so views that
are not live (reports, other pages, a restart) read values at most a minute old. Never the end-of-day archive.
The live 1m bars (live/bars.py) go to the intraday cache as source "live" (repo/intraday.store_live)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..analytics.calendar import now_ny
from ..jobs import hooks
from ..models import IntradayBar
from ..repo import halts as halts_repo
from ..repo import intraday as intraday_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..services import context as ctx


def store(quotes: list[dict[str, Any]], options: list[dict[str, Any]]) -> list[str]:
    """Blocking. Rows carry `_at` (epoch seconds), `source`, `delayed` and the changed fields. Returns the symbols whose
    halt state changed."""
    groups: dict[tuple[str, bool], list[dict]] = {}
    halts: dict[str, list[tuple[str, bool, datetime]]] = {}
    for q in quotes:
        row = {**q, "ts": datetime.fromtimestamp(q["_at"], timezone.utc)}
        groups.setdefault((q["source"], bool(q.get("delayed"))), []).append(row)
        if q.get("halted") is not None:
            halts.setdefault(q["source"], []).append((q["symbol"], bool(q["halted"]), row["ts"]))
    for (source, delayed), rows in groups.items():
        quotes_repo.merge_quotes(rows, source, delayed)
    if options:
        options_repo.patch_latest(options)
        ctx.forget_chains(options_repo.symbols_of([o["osi"] for o in options]).values())
    return [s for source, obs in halts.items() for s in halts_repo.record(obs, source)]


async def persist(quotes: list[dict[str, Any]], options: list[dict[str, Any]]) -> None:
    changed = await asyncio.to_thread(store, quotes, options)
    if changed:
        await hooks.fire(hooks.on_halt_change, now_ny().date(), changed)


async def store_bars(bars: dict[str, list[IntradayBar]]) -> None:
    await asyncio.to_thread(intraday_repo.store_live, bars, datetime.now(timezone.utc))
