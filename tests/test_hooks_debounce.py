"""jobs.hooks.Debounced: pushed data fires on_sweep_complete-style hooks at most once per interval,
coalescing days and symbols, without blocking the notifier."""
from __future__ import annotations

import asyncio
from datetime import date

from optionlens.jobs import hooks

D1, D2 = date(2026, 11, 19), date(2026, 11, 20)


async def test_coalesces_and_rate_limits():
    calls: list[tuple[date, list[str]]] = []
    d = hooks.Debounced([lambda day, syms: calls.append((day, syms))], interval_s=0.3, min_delay_s=0.05)
    await d.notify(D2, ["MSFT"])
    await d.notify(D2, ["AAPL", "MSFT"])
    await d.notify(D1, ["SPY"])
    assert calls == []                                   # nothing runs on the notifier's path
    await asyncio.sleep(0.12)
    assert calls == [(D1, ["SPY"]), (D2, ["AAPL", "MSFT"])]
    await d.notify(D2, ["NVDA"])                         # within the interval: held back
    await asyncio.sleep(0.12)
    assert len(calls) == 2
    await asyncio.sleep(0.25)
    assert calls[2:] == [(D2, ["NVDA"])]


async def test_notifications_during_a_fire_are_not_lost():
    calls: list[list[str]] = []
    d = hooks.Debounced([], interval_s=0.1, min_delay_s=0.01)

    async def slow(day, syms):
        calls.append(syms)
        if len(calls) == 1:
            await d.notify(day, ["LATE"])                # arrives while the hooks run
            await asyncio.sleep(0.02)

    d.hooks.append(slow)
    await d.notify(D1, ["A"])
    await asyncio.sleep(0.25)
    assert calls == [["A"], ["LATE"]]


async def test_failing_hook_does_not_stop_others():
    seen = []

    def boom(day, syms):
        raise RuntimeError("x")

    d = hooks.Debounced([boom, lambda day, syms: seen.append(syms)], interval_s=1, min_delay_s=0.01)
    await d.notify(D1, ["A"])
    await asyncio.sleep(0.1)
    assert seen == [["A"]]
