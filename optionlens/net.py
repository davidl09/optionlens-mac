"""Shared async HTTP client with per-host rate limiting and retries.

Each rate-limited host has a gate: token bucket (NASDAQ_RPS / CBOE_RPS), an optional concurrency cap
(CBOE_CONCURRENCY) and a host-wide cooldown that a 429 triggers (Retry-After, else escalating 30 s ->
240 s) so every pending request to that host pauses instead of hammering it.
Retries with exponential backoff on 5xx/timeouts/connection errors (3x) and on 429 (6x). Raises FetchError.
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
RETRIES_429 = 6
BACKOFF_S = 0.5
COOLDOWN_MIN_S = 30.0
COOLDOWN_MAX_S = 240.0
# A CDN block page (HTTP 403 with an HTML body, e.g. Cloudflare) means this IP is refused. Stop all
# traffic to that host family (fail fast) for a long pause, doubling while blocks persist.
BLOCK_PAUSE_S = 3600.0
BLOCK_PAUSE_MAX_S = 6 * 3600.0
RATE_LIMITED_HOSTS = ("nasdaq.com", "cboe.com")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class FetchError(Exception):
    """A remote fetch failed permanently. `status` is the HTTP status (None for transport errors);
    `blocked` is True when the host refused us with a CDN block page (or we are pausing after one)."""

    def __init__(self, url: str, status: int | None, message: str = "", blocked: bool = False):
        super().__init__(f"{status or 'error'} {url} {message}".strip())
        self.url = url
        self.status = status
        self.blocked = blocked


def _family(host: str) -> str | None:
    return next((h for h in RATE_LIMITED_HOSTS if host.endswith(h)), None)


# Process-wide (shared by every event loop / worker thread): host family -> (blocked_until, last pause).
_blocks: dict[str, tuple[float, float]] = {}


def blocked_until(host: str) -> float | None:
    """time.time() until which requests to this host family are suspended, or None."""
    fam = _family(host)
    until = _blocks.get(fam, (0.0, 0.0))[0] if fam else 0.0
    return until if until > time.time() else None


def _register_block(host: str) -> None:
    fam = _family(host)
    if fam is None:
        return
    _, last = _blocks.get(fam, (0.0, 0.0))
    pause = min(last * 2, BLOCK_PAUSE_MAX_S) if last else BLOCK_PAUSE_S
    _blocks[fam] = (time.time() + pause, pause)
    log.error("%s refused this host with a CDN block page; suspending its requests for %.0f min",
              fam, pause / 60)


def _clear_block_escalation(host: str) -> None:
    fam = _family(host)
    if fam in _blocks and _blocks[fam][0] <= time.time():
        del _blocks[fam]


def _is_block_page(r: httpx.Response) -> bool:
    return r.status_code == 403 and "html" in r.headers.get("content-type", "").lower()


class _TokenBucket:
    """`rate` tokens/s (> 0); holds at most max(1, rate) tokens so a sub-1 rate still yields a token."""

    def __init__(self, rate: float):
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        self.rate = rate
        self.capacity = max(1.0, rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class _Gate:
    """Rate limit + concurrency cap + 429 cooldown for one host family."""

    def __init__(self, rps: float, concurrency: int | None = None):
        self.bucket = _TokenBucket(rps)
        self.sem = asyncio.Semaphore(concurrency) if concurrency else None
        self.cooldown_until = 0.0
        self.cooldown_s = COOLDOWN_MIN_S

    async def _wait_cooldown(self) -> None:
        while (delay := self.cooldown_until - time.monotonic()) > 0:
            await asyncio.sleep(delay)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self._wait_cooldown()
        if self.sem is None:
            await self.bucket.acquire()
            yield
            return
        async with self.sem:
            await self._wait_cooldown()
            await self.bucket.acquire()
            yield

    def throttled(self, retry_after: str | None) -> None:
        try:
            delay = float(retry_after) if retry_after else self.cooldown_s
        except ValueError:
            delay = self.cooldown_s
        until = time.monotonic() + min(max(delay, 1.0), COOLDOWN_MAX_S)
        if until > self.cooldown_until:
            self.cooldown_until = until
            log.warning("rate limited; pausing host for %.0fs", until - time.monotonic())
        self.cooldown_s = min(self.cooldown_s * 2, COOLDOWN_MAX_S)

    def succeeded(self) -> None:
        if time.monotonic() > self.cooldown_until:
            self.cooldown_s = COOLDOWN_MIN_S


class _Net:
    """Client + per-host gates bound to one event loop."""

    def __init__(self) -> None:
        s = get_settings()
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                     "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True, timeout=TIMEOUT,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        self.gates = {"nasdaq.com": _Gate(s.nasdaq_rps),
                      "cboe.com": _Gate(s.cboe_rps, s.cboe_concurrency)}

    def gate(self, host: str) -> _Gate | None:
        return next((g for suffix, g in self.gates.items() if host.endswith(suffix)), None)

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        g = self.gate(host)
        if g is None:
            yield
        else:
            async with g.slot():
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
    gate = net.gate(host)
    last: FetchError | None = None
    attempt = throttles = 0
    while attempt <= RETRIES and throttles <= RETRIES_429:
        if blocked_until(host):
            raise FetchError(url, 403, "host suspended after a CDN block", blocked=True)
        if attempt:
            await asyncio.sleep(BACKOFF_S * 2 ** (attempt - 1))
        try:
            async with net.slot(host):
                r = await net.client.get(url, params=params)
                await r.aread()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = FetchError(url, None, type(e).__name__)
            log.debug("fetch retry %s: %s", url, e)
            attempt += 1
            continue
        if r.is_success:
            if gate:
                gate.succeeded()
            _clear_block_escalation(host)
            return r
        if _is_block_page(r):
            _register_block(host)
            raise FetchError(str(r.url), 403, "CDN block page", blocked=True)
        last = FetchError(str(r.url), r.status_code)
        if r.status_code == 429:
            throttles += 1
            if gate:
                gate.throttled(r.headers.get("Retry-After"))  # next slot() waits out the cooldown
            else:
                await asyncio.sleep(BACKOFF_S * 2 ** throttles)
            continue
        if not _retryable(r.status_code):
            break
        attempt += 1
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
