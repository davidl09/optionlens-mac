"""Pacing of Questrade market-data calls (docs/INTEGRATION.md §8).

Documented limits (questrade.com/api/documentation/rate-limiting, "Market Data calls"): 20 requests per second
and 15,000 per hour; every limited response carries X-RateLimit-Remaining (requests left against the current
limit) and X-RateLimit-Reset (Unix time the current limit expires); over the limit the API answers 429 with the
same headers. The gate uses at most SHARE (70%) of each limit:
- calls are spaced 1 / (SHARE x 20) s apart, and at most SHARE x 15,000 start in any rolling hour;
- the headers drive it too: once the calls used against the current limit reach SHARE of it, the gate pauses
  until its reset. The window is the hourly one when the reset is more than WINDOW_SPLIT_S away, else the
  per-second one (the docs do not say which limit the headers describe);
- a 429 pauses until the reset (else BACKOFF_429_S).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping

PER_SECOND = 20
PER_HOUR = 15_000
SHARE = 0.70
WINDOW_SPLIT_S = 2.0
BACKOFF_429_S = 5.0
HOUR_S = 3600.0


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class RateGate:
    def __init__(self, per_second: int = PER_SECOND, per_hour: int = PER_HOUR, share: float = SHARE,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep):
        self.per_second, self.per_hour, self.share = per_second, per_hour, share
        self.clock, self.sleep = clock, sleep
        self.interval = 1.0 / (share * per_second)
        self.hour_budget = int(share * per_hour)
        self._calls: deque[float] = deque()
        self._next_at = 0.0
        self._pause_until = 0.0
        self.remaining: int | None = None      # last X-RateLimit-Remaining
        self.reset_at: float | None = None     # last X-RateLimit-Reset

    def _hour_free_at(self, now: float) -> float:
        while self._calls and self._calls[0] <= now - HOUR_S:
            self._calls.popleft()
        return self._calls[0] + HOUR_S if len(self._calls) >= self.hour_budget else 0.0

    async def acquire(self) -> None:
        """Wait for the next call slot (see module doc)."""
        while True:
            now = self.clock()
            at = max(self._pause_until, self._next_at, self._hour_free_at(now))
            if at <= now:
                self._next_at = now + self.interval
                self._calls.append(now)
                return
            await self.sleep(at - now)

    def observe(self, headers: Mapping[str, str], status: int) -> None:
        """Feed a response's rate-limit headers (and a 429) into the gate."""
        now = self.clock()
        try:
            remaining = int(headers["X-RateLimit-Remaining"])
            reset = float(headers["X-RateLimit-Reset"])
        except (KeyError, ValueError, TypeError):
            remaining = None
            reset = None
        if remaining is not None and reset is not None:
            self.remaining, self.reset_at = remaining, reset
            limit = self.per_hour if reset - now > WINDOW_SPLIT_S else self.per_second
            if limit - remaining >= self.share * limit and reset > now:
                self._pause_until = max(self._pause_until, reset)
        if status == 429:
            until = reset if reset is not None and reset > now else now + BACKOFF_429_S
            self._pause_until = max(self._pause_until, until)

    def stats(self) -> dict:
        """Calls in the last hour against the gate's hourly budget, and the API's last reported headers."""
        now = self.clock()
        self._hour_free_at(now)
        return {"calls_last_hour": len(self._calls), "hour_budget": self.hour_budget,
                "remaining": self.remaining, "reset_at": _iso(self.reset_at),
                "paused_until": _iso(self._pause_until) if self._pause_until > now else None}
