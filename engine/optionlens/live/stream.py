"""Questrade Level 1 streaming (questrade.com/api/documentation/streaming, read 2026-11-24; docs/INTEGRATION.md §9).

Documented: a quote request with `stream=true` and `mode=WebSocket` (GET v1/markets/quotes?ids=…; for options the
JSON body `"stream": true, "mode": "WebSocket"` of POST v1/markets/quotes/options) answers {"streamPort": N}; on a
WebSocket the access token is sent as the first message "without any delimiters" and without the word Bearer; a
recognized token is answered {"success": true}. "Keeping a socket open does not extend your session": the socket is
dropped when the access token's session expires, so a stream is renewed (new port requests with a fresh token)
RENEW_MARGIN_S before the token expires, and at once when the token was refreshed meanwhile (e.g. by a sweep).

Verified live (v0.1.12, a real-time account, 2026-11-24): the equity and the option stream requests answer the SAME
port; Questrade allows ONE streaming connection per session: opening a second closes the first (close code 1005); the
one connection carries both {"quotes": [...]} (equities) and {"optionQuotes": [...]} (options) messages, fields
possibly partial (callers merge them per symbolId). So the stream requests only register ids on the session's port:
one run() issues the request of every kind, then opens ONE socket; a changed set needs new requests and a new socket.

Not documented (taken from client libraries): the socket URL (the API server's host, the stream port and a request's
path and query). The first LOG_FIRST messages of every connection are logged (INFO, scrubbed, truncated); the access
token is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from ..sources.questrade.auth import TokenManager
from ..sources.questrade.client import USER_AGENT, QuestradeClient

log = logging.getLogger("optionlens.live.stream")

Kind = Literal["equity", "option"]
KINDS: tuple[Kind, ...] = ("equity", "option")
_KEY: dict[str, Kind] = {"quotes": "equity", "optionQuotes": "option"}        # message key -> kind
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


def dispatch(msg: Any) -> list[tuple[Kind, list[dict]]]:
    """The quotes of one stream message per kind ("quotes" -> equity, "optionQuotes" -> option; a message may hold
    both); quotes without a symbolId are dropped."""
    if not isinstance(msg, dict):
        return []
    out = []
    for key, kind in _KEY.items():
        if isinstance(msg.get(key), list):
            if quotes := [q for q in msg[key] if isinstance(q, dict) and q.get("symbolId") is not None]:
                out.append((kind, quotes))
    return out


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
    """One stream connection per run() call (Questrade allows one per session)."""

    def __init__(self, api: QuestradeClient, tokens: TokenManager, connect: Callable[..., Any] = ws_connect,
                 scheme: str = "wss", clock: Callable[[], float] = time.time):
        self.api, self.tokens, self.connect, self.scheme, self.clock = api, tokens, connect, scheme, clock

    async def _port(self, kind: Kind, ids: Sequence[int]) -> tuple[int, str]:
        """Register `ids` for streaming: (the stream port, the request's path)."""
        if kind == "equity":
            query = {"ids": ",".join(map(str, ids)), "stream": "true", "mode": "WebSocket"}
            r = await self.api.get("markets/quotes", query)
            path = f"/v1/markets/quotes?{urlencode(query, safe=',')}"
        else:
            r = await self.api.post("markets/quotes/options",
                                    {"optionIds": list(ids), "stream": True, "mode": "WebSocket"})
            path = "/v1/markets/quotes/options"
        try:
            return int(r["streamPort"]), path
        except (KeyError, TypeError, ValueError) as e:
            raise HandshakeError(f"Questrade answered the {kind} stream request without a streamPort") from e

    async def run(self, subs: Mapping[Kind, Sequence[int]], on_quotes: Callable[[Kind, list[dict]], None],
                  on_open: Callable[[int], None] = lambda port: None, on_message: Callable[[], None] = lambda: None,
                  silent_s: float | None = None) -> None:
        """Register the ids of every kind in `subs` (its non-empty entries; one stream request per kind), open ONE
        socket to the port they share and pass every message's quotes to on_quotes(kind, quotes) (on_message counts
        every message). Returns normally when it should be renewed: the access token is about to expire or was
        refreshed. Raises HandshakeError (the requests answered different ports, or no authenticated stream),
        StreamSilent (no message within `silent_s` of the handshake, when given), StreamDropped (closed or failed
        after the handshake); QuestradeError / TokenError from the stream requests as the REST client raises them."""
        wanted = {k: list(subs[k]) for k in KINDS if subs.get(k)}
        if not wanted:
            raise ValueError("nothing to stream")
        got = {kind: await self._port(kind, ids) for kind, ids in wanted.items()}
        ports = {port for port, _ in got.values()}
        if len(ports) != 1:
            shown = ", ".join(f"{k} {p}" for k, (p, _) in got.items())
            raise HandshakeError(f"Questrade answered different stream ports ({shown}); one connection per session "
                                 "cannot carry both")
        port, path = next(iter(got.values()))     # the socket path: the first request's (equities when wanted)
        access = await self.tokens.access()        # the token the requests used (fresh if it was refreshed)
        host = urlsplit(access.api_server).hostname
        url = f"{self.scheme}://{host}:{port}{path}"
        stop_at = access.expires_at - RENEW_MARGIN_S
        sizes = ", ".join(f"{len(ids)} {k}" for k, ids in wanted.items())
        opened = False

        def deliver(msg: Any) -> None:
            for kind, quotes in dispatch(msg):
                on_quotes(kind, quotes)
        try:
            async with self.connect(url, user_agent_header=USER_AGENT, open_timeout=OPEN_TIMEOUT_S,
                                    max_size=8 * 2**20) as ws:
                await ws.send(access.token)
                raw = await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT_S)
                try:
                    first = json.loads(raw)
                except ValueError:
                    first = None
                if not (isinstance(first, dict) and first.get("success") is True) and not dispatch(first):
                    why = (first.get("message") if isinstance(first, dict) else None) or scrub(raw, access.token)
                    log.info("live stream: handshake refused on port %d: %s", port, why)
                    raise HandshakeError(f"Questrade did not accept the stream token ({why})")
                log.info("live stream: handshake ok on port %d (%s ids)", port, sizes)
                opened = True
                on_open(port)
                opened_at, seen = self.clock(), 0
                if dispatch(first):
                    seen = 1
                    on_message()
                    log.info("live stream message 1: %s", scrub(raw, access.token))
                    deliver(first)
                while True:
                    now = self.clock()
                    if now >= stop_at:
                        log.info("live stream: renewing before the access token expires")
                        return
                    if self.tokens.current() is not access:
                        log.info("live stream: the access token was refreshed: renewing")
                        return
                    wait = min(stop_at - now, TOKEN_CHECK_S)
                    if silent_s is not None and seen == 0:
                        if now - opened_at >= silent_s:
                            raise StreamSilent(f"stream: no message within {silent_s:.0f} s")
                        wait = min(wait, opened_at + silent_s - now)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), max(0.0, wait))
                    except TimeoutError:
                        continue
                    seen += 1
                    on_message()
                    if seen <= LOG_FIRST:
                        log.info("live stream message %d: %s", seen, scrub(raw, access.token))
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    deliver(msg)
        except StreamError:
            raise
        except (OSError, WebSocketException, TimeoutError, ValueError) as e:
            if not opened:
                log.info("live stream: handshake failed on port %d: %s", port, type(e).__name__)
            cls = StreamDropped if opened else HandshakeError
            raise cls(f"stream: {type(e).__name__}: {e}") from e
