"""Questrade REST client: bearer auth from the TokenManager, the RateGate before every call, retries.

- 401 (code 1017 "Access token is invalid"): the access token is dropped, refreshed once and the call retried;
- 429 (code 1006 "Rate limit exceeded"): the gate pauses until X-RateLimit-Reset, then the call is retried
  (at most RETRIES_429 times);
- 5xx / timeouts / connection errors: exponential backoff, RETRIES times;
- anything else, and a token problem (auth.TokenError): net.FetchError with Questrade's `code`/`message`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from ...net import FetchError
from .auth import TokenError, TokenManager
from .ratelimit import RateGate

log = logging.getLogger(__name__)

RETRIES = 3
RETRIES_429 = 5
BACKOFF_S = 0.5
USER_AGENT = "OptionLens"


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
        return f"code {body.get('code')}: {body.get('message')}" if isinstance(body, dict) else ""
    except ValueError:
        return ""


class QuestradeClient:
    def __init__(self, tokens: TokenManager, gate: RateGate, http: Callable[[], httpx.AsyncClient],
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep):
        self.tokens, self.gate, self.http, self.sleep = tokens, gate, http, sleep

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        return await self._request("POST", path, json=body)

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        failures = throttles = 0
        reauthed = False
        while True:
            try:
                access = await self.tokens.access()
            except TokenError as e:
                raise FetchError(f"questrade:{path}", None, str(e)) from e
            url = f"{access.api_server}/v1/{path}"
            await self.gate.acquire()
            try:
                r = await self.http().request(method, url, headers={
                    "Authorization": f"Bearer {access.token}", "Accept": "application/json",
                    "User-Agent": USER_AGENT}, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                failures += 1
                if failures > RETRIES:
                    raise FetchError(url, None, type(e).__name__) from e
                await self.sleep(BACKOFF_S * 2 ** (failures - 1))
                continue
            self.gate.observe(r.headers, r.status_code)
            if r.status_code == 401 and not reauthed:
                self.tokens.invalidate(access)
                reauthed = True
                continue
            if r.status_code == 429:
                throttles += 1
                if throttles > RETRIES_429:
                    raise FetchError(url, 429, _error_text(r))
                log.warning("questrade: rate limited; pausing until the limit resets")
                continue
            if r.status_code >= 500:
                failures += 1
                if failures > RETRIES:
                    raise FetchError(url, r.status_code, _error_text(r))
                await self.sleep(BACKOFF_S * 2 ** (failures - 1))
                continue
            if not r.is_success:
                raise FetchError(url, r.status_code, _error_text(r))
            try:
                return r.json()
            except ValueError as e:
                raise FetchError(url, r.status_code, "invalid JSON") from e
