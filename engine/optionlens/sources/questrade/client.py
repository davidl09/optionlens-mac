"""Questrade REST client: bearer auth from the TokenManager, the RateGate before every call, retries. Failures
raise errors.QuestradeError, classified per errors.py:

- 401 (code 1017 "Access token is invalid"): the access token is dropped, refreshed once and the call retried; a
  second 401, any 403 (e.g. 1016 "out of allowed OAuth scopes") and codes 1014-1017: the token manager is told the
  source needs attention (TokenManager.flag: no further calls until a new token is pasted) and the call fails;
- 429 (code 1006 "Rate limit exceeded"): the gate pauses until X-RateLimit-Reset, then the call is retried
  (at most RETRIES_429 times);
- 5xx / timeouts / connection errors: exponential backoff, RETRIES times;
- anything else fails at once; a token problem (auth.TokenError) too.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from .auth import NeedsAttention, TokenError, TokenManager, TokenRejected
from .errors import QuestradeError
from .ratelimit import RateGate

log = logging.getLogger(__name__)

RETRIES = 3
RETRIES_429 = 5
BACKOFF_S = 0.5
USER_AGENT = "OptionLens"


def _error(url: str, r: httpx.Response) -> QuestradeError:
    """The QuestradeError of a failed response (Questrade's {"code", "message"} when the body has them)."""
    try:
        body = r.json()
    except ValueError:
        body = None
    code = message = None
    if isinstance(body, dict):
        try:
            code = int(body["code"]) if body.get("code") is not None else None
        except (TypeError, ValueError):
            code = None
        message = str(body["message"]) if body.get("message") is not None else None
    return QuestradeError(url, r.status_code, code, message)


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
                kind = "auth" if isinstance(e, (TokenRejected, NeedsAttention)) else "transient"
                raise QuestradeError(f"questrade:{path}", None, message=str(e), kind=kind,
                                     reason_text=f"login: {e}") from e
            url = f"{access.api_server}/v1/{path}"
            await self.gate.acquire()
            try:
                r = await self.http().request(method, url, headers={
                    "Authorization": f"Bearer {access.token}", "Accept": "application/json",
                    "User-Agent": USER_AGENT}, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                failures += 1
                if failures > RETRIES:
                    raise QuestradeError(url, None, message=f"timeout or connection error ({type(e).__name__})",
                                         kind="transient", reason_text=f"no response ({type(e).__name__})") from e
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
                    raise _error(url, r)
                log.warning("questrade: rate limited; pausing until the limit resets")
                continue
            if r.status_code >= 500:
                failures += 1
                if failures > RETRIES:
                    raise _error(url, r)
                await self.sleep(BACKOFF_S * 2 ** (failures - 1))
                continue
            if not r.is_success:
                err = _error(url, r)
                if err.kind == "auth":   # circuit breaker: every further call would fail the same way
                    self.tokens.flag(err.attention or err.reason, err.reason)
                    log.warning("questrade: %s; no further calls until a new token is pasted", err.reason)
                raise err
            try:
                return r.json()
            except ValueError as e:
                raise QuestradeError(url, r.status_code, message="invalid JSON", kind="transient") from e
