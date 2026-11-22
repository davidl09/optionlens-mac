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

from ..models import ChainSnapshot

log = logging.getLogger("optionlens.jobs.hooks")

Hook = Callable[[date, list[str]], Any]
on_sweep_complete: list[Hook] = []   # after every sweep (incl. the EOD final sweep): symbols refreshed
on_eod: list[Hook] = []              # after option_eod + daily_bars were written for `day`
# Applied in order to a server-side source's chain (sync, in a worker thread) before it is stored: the
# services layer completes IV and greeks the same way as for pushed chains (services.ingest.complete_chain).
chain_preparers: list[Callable[[ChainSnapshot, str], ChainSnapshot]] = []


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


class Debounced:
    """Fires `hooks` at most once per `interval_s` with the days/symbols collected since the last fire
    (coalesced), off the caller's path: `notify` only records and schedules on the running event loop;
    the fire happens `min_delay_s` after the first notify (or when the interval allows)."""

    def __init__(self, hooks: list[Hook], interval_s: float, min_delay_s: float = 5.0,
                 clock: Callable[[], float] | None = None):
        self.hooks, self.interval_s, self.min_delay_s = hooks, interval_s, min_delay_s
        self.clock = clock or (lambda: asyncio.get_running_loop().time())
        self.pending: dict[date, set[str]] = {}
        self.last_fired: float | None = None
        self.task: asyncio.Task | None = None

    async def notify(self, day: date, symbols: list[str]) -> None:
        self.pending.setdefault(day, set()).update(symbols)
        if self.task is None or self.task.done():
            due = self.min_delay_s if self.last_fired is None else \
                max(self.min_delay_s, self.last_fired + self.interval_s - self.clock())
            self.task = asyncio.get_running_loop().create_task(self._fire_after(due))

    async def _fire_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        batch, self.pending = self.pending, {}
        self.last_fired = self.clock()
        for day in sorted(batch):
            await fire(self.hooks, day, sorted(batch[day]))
        if self.pending:   # notified while the hooks ran
            self.task = asyncio.get_running_loop().create_task(
                self._fire_after(max(self.min_delay_s, self.interval_s)))
