"""Extension points for the services layer (alerts, report snapshots, digests).

Register callables `fn(day: date, symbols: list[str])` (sync -> run in a thread, or async):
    from optionlens.jobs import hooks
    hooks.on_sweep_complete.append(evaluate_alerts)
    hooks.on_eod.append(store_report_snapshots)
Hook exceptions are logged and never stop the job.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import date
from typing import Any, Callable

log = logging.getLogger("optionlens.jobs.hooks")

Hook = Callable[[date, list[str]], Any]
on_sweep_complete: list[Hook] = []   # after every sweep (incl. the EOD final sweep): symbols refreshed
on_eod: list[Hook] = []              # after option_eod + daily_bars were written for `day`


async def fire(hooks: list[Hook], day: date, symbols: list[str]) -> int:
    """Run hooks in registration order. Returns how many failed."""
    failed = 0
    for h in list(hooks):
        try:
            if inspect.iscoroutinefunction(h):
                await h(day, symbols)
            else:
                await asyncio.to_thread(h, day, symbols)
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("hook %s failed", getattr(h, "__name__", h))
    return failed
