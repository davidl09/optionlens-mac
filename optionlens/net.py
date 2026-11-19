"""Shared async HTTP client with per-host rate limiting and retries.

Nasdaq: token bucket (NASDAQ_RPS). CBOE: concurrency semaphore (CBOE_CONCURRENCY).
Retries 3x with exponential backoff on 429/5xx/timeouts/connection errors. Raises FetchError.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
RETRIES = 3
BACKOFF_S = 0.5
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class FetchError(Exception):
    """A remote fetch failed permanently. `status` is the HTTP status (None for transport errors)."""

    def __init__(self, url: str, status: int | None, message: str = ""):
        super().__init__(f"{status or 'error'} {url} {message}".strip())
        self.url = url
        self.status = status


class _TokenBucket:
    def __init__(self, rate: float):
        self.rate = rate
        self.tokens = rate
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class _Net:
    """Client + limiters bound to one event loop."""

    def __init__(self) -> None:
        s = get_settings()
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                     "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True, timeout=TIMEOUT,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        self.nasdaq = _TokenBucket(s.nasdaq_rps)
        self.cboe = asyncio.Semaphore(s.cboe_concurrency)

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        if host.endswith("nasdaq.com"):
            await self.nasdaq.acquire()
            yield
        elif host.endswith("cboe.com"):
            async with self.cboe:
                yield
        else:
            yield


_nets: dict[asyncio.AbstractEventLoop, _Net] = {}


def _net() -> _Net:
    loop = asyncio.get_running_loop()
    n = _nets.get(loop)
    if n is None:
        for dead in [lp for lp in _nets if lp.is_closed()]:
            _nets.pop(dead)
        n = _nets[loop] = _Net()
    return n


async def aclose() -> None:
    """Close the client bound to the running loop."""
    n = _nets.pop(asyncio.get_running_loop(), None)
    if n:
        await n.client.aclose()


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


async def get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
    """GET with rate limiting + retries; returns a 2xx response or raises FetchError."""
    net = _net()
    host = httpx.URL(url).host
    last: FetchError | None = None
    for attempt in range(RETRIES + 1):
        if attempt:
            await asyncio.sleep(BACKOFF_S * 2 ** (attempt - 1))
        try:
            async with net.slot(host):
                r = await net.client.get(url, params=params)
                await r.aread()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = FetchError(url, None, type(e).__name__)
            log.debug("fetch retry %s: %s", url, e)
            continue
        if r.is_success:
            return r
        last = FetchError(str(r.url), r.status_code)
        if not _retryable(r.status_code):
            break
    assert last is not None
    raise last


async def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET and decode JSON (FetchError on bad JSON)."""
    r = await get(url, params)
    try:
        return r.json()
    except ValueError as e:
        raise FetchError(url, r.status_code, f"invalid JSON: {e}") from e


async def get_text(url: str, params: dict[str, Any] | None = None) -> str:
    """GET and return body text."""
    return (await get(url, params)).text
