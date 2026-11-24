"""Questrade Level 1 streaming (questrade.com/api/documentation/streaming, read 2026-11-24; docs/INTEGRATION.md §9).

Documented: a quote request with `stream=true` and `mode=WebSocket` (GET v1/markets/quotes?ids=…; for options the
JSON body `"stream": true, "mode": "WebSocket"` of POST v1/markets/quotes/options) answers {"streamPort": N}; on a
WebSocket the access token is sent as the first message "without any delimiters" and without the word Bearer; a
recognized token is answered {"success": true}. "Keeping a socket open does not extend your session": the socket is
dropped when the access token's session expires, so a stream is renewed (new port request with a fresh token)
RENEW_MARGIN_S before the token expires, and at once when the token was refreshed meanwhile (e.g. by a sweep).
A stream carries the ids of its port request: a changed set needs a new port request and a new socket.

Not documented (taken from client libraries, unverified against the live API): the socket URL (the API server's host,
the stream port and the request's path and query), and that quote messages carry the REST shape ({"quotes": [...]},
fields possibly partial: callers merge them per symbolId). For the first live verification the first LOG_FIRST
messages of every connection are logged (INFO, scrubbed, truncated); the access token is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
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
TOKEN_CHECK_S = 2.0          # how often an open stream checks whether the access token was refreshed
LOG_FIRST = 3                # raw messages logged per connection
LOG_CHARS = 300
_SENSITIVE = re.compile(r"token|secret|session|account|password|key", re.I)


class StreamError(Exception):
    """The stream could not be opened or dropped (the caller reconnects or falls back)."""


class HandshakeError(StreamError):
    """No authenticated stream: the socket did not open, or the token was not answered with {"success": true}."""


class StreamDropped(StreamError):
    """An authenticated stream closed or failed."""


class StreamSilent(StreamError):
    """An authenticated stream sent no message within the expected time."""


def _quotes(msg: Any) -> list[dict]:
    if not isinstance(msg, dict):
        return []
    for k in ("quotes", "optionQuotes"):
        if isinstance(msg.get(k), list):
            return [q for q in msg[k] if isinstance(q, dict) and q.get("symbolId") is not None]
    return []


def _strip(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: "***" if _SENSITIVE.search(str(k)) else _strip(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


def scrub(raw: str | bytes, secret: str | None) -> str:
    """A raw stream message for the log: fields whose names could hold secrets masked, `secret` (the access token)
    never shown, at most LOG_CHARS characters."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        text = json.dumps(_strip(json.loads(text)), separators=(",", ":"))
    except ValueError:
        pass
    if secret:
        text = text.replace(secret, "***")
    return text if len(text) <= LOG_CHARS else text[:LOG_CHARS] + "…"


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
            raise HandshakeError("Questrade answered the stream request without a streamPort") from e

    async def run(self, kind: Kind, ids: list[int], on_quotes: Callable[[list[dict]], None],
                  on_open: Callable[[int], None] = lambda port: None, on_message: Callable[[], None] = lambda: None,
                  silent_s: float | None = None) -> None:
        """Open a stream of `ids` and pass every quote message to on_quotes (on_message counts every message).
        Returns normally when it should be renewed: the access token is about to expire or was refreshed. Raises
        HandshakeError (no authenticated stream), StreamSilent (no message within `silent_s` of the handshake, when
        given), StreamDropped (closed or failed after the handshake); QuestradeError / TokenError from the port
        request as the REST client raises them."""
        port, path = await self._port(kind, ids)
        access = await self.tokens.access()        # the token the port request used (fresh if it was refreshed)
        host = urlsplit(access.api_server).hostname
        url = f"{self.scheme}://{host}:{port}{path}"
        stop_at = access.expires_at - RENEW_MARGIN_S
        opened = False
        try:
            async with self.connect(url, user_agent_header=USER_AGENT, open_timeout=OPEN_TIMEOUT_S,
                                    max_size=8 * 2**20) as ws:
                await ws.send(access.token)
                raw = await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT_S)
                try:
                    first = json.loads(raw)
                except ValueError:
                    first = None
                if not (isinstance(first, dict) and first.get("success") is True) and not _quotes(first):
                    why = (first.get("message") if isinstance(first, dict) else None) or scrub(raw, access.token)
                    log.info("live stream %s: handshake refused on port %d: %s", kind, port, why)
                    raise HandshakeError(f"Questrade did not accept the stream token ({why})")
                log.info("live stream %s: handshake ok on port %d (%d ids)", kind, port, len(ids))
                opened = True
                on_open(port)
                opened_at, seen = self.clock(), 0
                if _quotes(first):
                    seen = 1
                    on_message()
                    log.info("live stream %s message 1: %s", kind, scrub(raw, access.token))
                    on_quotes(_quotes(first))
                while True:
                    now = self.clock()
                    if now >= stop_at:
                        log.info("live stream %s: renewing before the access token expires", kind)
                        return
                    if self.tokens.current() is not access:
                        log.info("live stream %s: the access token was refreshed: renewing", kind)
                        return
                    wait = min(stop_at - now, TOKEN_CHECK_S)
                    if silent_s is not None and seen == 0:
                        if now - opened_at >= silent_s:
                            raise StreamSilent(f"{kind} stream: no message within {silent_s:.0f} s")
                        wait = min(wait, opened_at + silent_s - now)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), max(0.0, wait))
                    except TimeoutError:
                        continue
                    seen += 1
                    on_message()
                    if seen <= LOG_FIRST:
                        log.info("live stream %s message %d: %s", kind, seen, scrub(raw, access.token))
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if quotes := _quotes(msg):
                        on_quotes(quotes)
        except StreamError:
            raise
        except (OSError, WebSocketException, TimeoutError, ValueError) as e:
            if not opened:
                log.info("live stream %s: handshake failed on port %d: %s", kind, port, type(e).__name__)
            cls = StreamDropped if opened else HandshakeError
            raise cls(f"{kind} stream: {type(e).__name__}: {e}") from e
