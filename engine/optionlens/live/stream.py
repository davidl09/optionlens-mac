"""Questrade Level 1 streaming (questrade.com/api/documentation/streaming, read 2026-11-24; docs/INTEGRATION.md §9).

Documented: a quote request with `stream=true` and `mode=WebSocket` (GET v1/markets/quotes?ids=…; for options the
JSON body `"stream": true, "mode": "WebSocket"` of POST v1/markets/quotes/options) answers {"streamPort": N}; on a
WebSocket the access token is sent as the first message "without any delimiters" and without the word Bearer; a
recognized token is answered {"success": true}. "Keeping a socket open does not extend your session": the socket is
dropped when the access token's session expires, so a stream is renewed (new port request with a fresh token)
RENEW_MARGIN_S before the token expires.

Not documented (taken from client libraries, unverified against the live API): the socket URL (the API server's host,
the stream port and the request's path and query), and that quote messages carry the REST shape ({"quotes": [...]},
fields possibly partial: callers merge them per symbolId).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Literal
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from ..sources.questrade.auth import TokenManager
from ..sources.questrade.client import USER_AGENT, QuestradeClient

log = logging.getLogger("optionlens.live.stream")

Kind = Literal["equity", "option"]
RENEW_MARGIN_S = 60.0
AUTH_TIMEOUT_S = 10.0
OPEN_TIMEOUT_S = 10.0


class StreamError(Exception):
    """The stream could not be opened or dropped (the caller reconnects)."""


def _quotes(msg: Any) -> list[dict]:
    if not isinstance(msg, dict):
        return []
    for k in ("quotes", "optionQuotes"):
        if isinstance(msg.get(k), list):
            return [q for q in msg[k] if isinstance(q, dict) and q.get("symbolId") is not None]
    return []


class QuoteStream:
    """One stream connection at a time per run() call."""

    def __init__(self, api: QuestradeClient, tokens: TokenManager, connect: Callable[..., Any] = ws_connect,
                 scheme: str = "wss", clock: Callable[[], float] = time.time):
        self.api, self.tokens, self.connect, self.scheme, self.clock = api, tokens, connect, scheme, clock

    async def _port(self, kind: Kind, ids: list[int]) -> tuple[int, str]:
        if kind == "equity":
            query = {"ids": ",".join(map(str, ids)), "stream": "true", "mode": "WebSocket"}
            r = await self.api.get("markets/quotes", query)
            path = f"/v1/markets/quotes?{urlencode(query, safe=',')}"
        else:
            r = await self.api.post("markets/quotes/options", {"optionIds": ids, "stream": True, "mode": "WebSocket"})
            path = "/v1/markets/quotes/options"
        try:
            return int(r["streamPort"]), path
        except (KeyError, TypeError, ValueError) as e:
            raise StreamError("Questrade answered the stream request without a streamPort") from e

    async def run(self, kind: Kind, ids: list[int], on_quotes: Callable[[list[dict]], None],
                  on_open: Callable[[], None] = lambda: None) -> None:
        """Open a stream of `ids`, pass every quote message to on_quotes, and return normally when the access token
        is about to expire (renew by calling again). Raises StreamError when it cannot open or drops; QuestradeError /
        TokenError from the port request as the REST client raises them."""
        port, path = await self._port(kind, ids)
        access = await self.tokens.access()        # the token the port request used (fresh if it was refreshed)
        host = urlsplit(access.api_server).hostname
        url = f"{self.scheme}://{host}:{port}{path}"
        stop_at = access.expires_at - RENEW_MARGIN_S
        try:
            async with self.connect(url, user_agent_header=USER_AGENT, open_timeout=OPEN_TIMEOUT_S,
                                    max_size=8 * 2**20) as ws:
                await ws.send(access.token)
                first = json.loads(await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT_S))
                if isinstance(first, dict) and first.get("success") is not True and not _quotes(first):
                    raise StreamError(f"Questrade did not accept the stream token ({first.get('message') or first})")
                on_open()
                if _quotes(first):
                    on_quotes(_quotes(first))
                while (remaining := stop_at - self.clock()) > 0:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), remaining)
                    except TimeoutError:
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        log.debug("live stream: ignoring a non-JSON message")
                        continue
                    if quotes := _quotes(msg):
                        on_quotes(quotes)
        except (OSError, WebSocketException, TimeoutError, ValueError) as e:
            raise StreamError(f"stream {kind}: {type(e).__name__}: {e}") from e
