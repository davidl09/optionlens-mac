"""Questrade Level 1 streaming (live/stream.py, live/questrade.py stream mode) against a local WebSocket server that
plays Questrade's documented handshake: the port request, the access token as the first message, {"success": true},
then quote messages. Covered: the handshake, drops and reconnects, renewal before the access token expires and after
it was refreshed elsewhere, the fallbacks to polling (failed handshake, silent stream, drop storm) and the retry,
reopening only the stream whose set changed, switching the mode at runtime both ways, and the scrubbed diagnostics."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import pytest
from websockets.asyncio.server import serve

from optionlens.analytics.calendar import NY
from optionlens.live import questrade as qlive
from optionlens.live import stream as stream_mod
from optionlens.live.stream import HandshakeError, QuoteStream, StreamDropped, scrub
from optionlens.live.subscriptions import Wanted
from optionlens.sources.questrade import source as source_mod
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade, option_id

C1 = "XYZ261120C00100000"
C2 = "XYZ261120P00110000"


class FakeStreamServer:
    """Accepts sockets on 127.0.0.1 and plays one connection per the current settings."""

    def __init__(self, fake: FakeQuestrade):
        self.fake = fake
        self.tokens: list[str] = []
        self.paths: list[str] = []
        self.user_agents: list[str | None] = []
        self.closed = 0
        self.refuse = False
        self.drop_after: int | None = None       # close the socket after this many quote messages
        self.messages: list[dict] = [{"quotes": [{"symbolId": 1000, "lastTradePrice": 100.5}]}]
        self.tick_s: float | None = None         # then keep sending the last message every tick_s

    async def handler(self, ws):
        self.paths.append(ws.request.path)
        self.user_agents.append(ws.request.headers.get("User-Agent"))
        try:
            token = await ws.recv()
            self.tokens.append(token)
            if self.refuse or token != self.fake.access:
                await ws.send(json.dumps({"success": False, "code": 1017, "message": "Access token is invalid"}))
                await ws.close()
                return
            await ws.send(json.dumps({"success": True}))
            for m in self.messages[:self.drop_after]:
                await ws.send(json.dumps(m))
            if self.drop_after is not None:
                await ws.close()
                return
            while self.tick_s is not None and self.messages:
                await asyncio.sleep(self.tick_s)
                await ws.send(json.dumps(self.messages[-1]))
            await ws.wait_closed()
        finally:
            self.closed += 1

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.fake.stream_port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
def qt(tmp_path, monkeypatch):
    fake = FakeQuestrade()
    src = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=fake.http)
    monkeypatch.setattr(source_mod, "now_ny", lambda: datetime(2026, 11, 19, 10, 0, tzinfo=NY))
    monkeypatch.setattr(stream_mod, "TOKEN_CHECK_S", 0.05)
    return fake, src


def stream_for(src) -> QuoteStream:
    """A QuoteStream whose socket goes to 127.0.0.1 (the API server's host is a questrade.com name) over ws://."""
    from websockets.asyncio.client import connect

    def local(url, **kw):
        host = url.split("://", 1)[1].split(":", 1)[0]
        return connect(url.replace(host, "127.0.0.1", 1), **kw)
    return QuoteStream(src.api, src.tokens, connect=local, scheme="ws")


class Shift:
    """The real clock plus an offset (to jump past the retry delay)."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def driver(src, published=None, mode="stream", regular=False, clock=None):
    return qlive.QuestradeLive(src, (lambda s, q, o: published.append((dict(q), dict(o)))) if published is not None
                               else (lambda *a: None), mode=mode, sleep=lambda s: asyncio.sleep(0.01),
                               stream=stream_for(src), regular=lambda: regular, clock=clock or time.time)


async def until(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def stop(task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def port_requests(fake) -> int:
    return sum(1 for r in fake.requests if r.url.params.get("stream") == "true"
               or (r.method == "POST" and b'"stream"' in r.content))


# ----------------------------------------------------------------------------- the client
async def test_handshake_token_first_then_quotes(qt, caplog):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    caplog.set_level(logging.INFO, "optionlens.live.stream")
    async with FakeStreamServer(fake) as srv:
        got, opened, counted = [], [], []
        task = asyncio.create_task(stream_for(src).run("equity", [1000], got.extend, opened.append,
                                                       lambda: counted.append(1)))
        await until(lambda: got)
        await stop(task)
    assert srv.tokens == [fake.access]                                   # the bare token, no "Bearer"
    assert opened == [fake.stream_port] and got == [{"symbolId": 1000, "lastTradePrice": 100.5}] and counted == [1]
    assert srv.paths == ["/v1/markets/quotes?ids=1000&stream=true&mode=WebSocket"]
    assert srv.user_agents == ["OptionLens"]
    port_req = next(r for r in fake.requests if r.url.params.get("stream") == "true")
    assert port_req.url.params["mode"] == "WebSocket" and port_req.url.params["ids"] == "1000"
    logs = caplog.text
    assert f"handshake ok on port {fake.stream_port} (1 ids)" in logs
    assert 'message 1: {"quotes":[{"symbolId":1000,"lastTradePrice":100.5}]}' in logs
    assert fake.access not in logs                                       # never the access token


async def test_option_stream_port_request_is_a_post_with_option_ids(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.messages = [{"optionQuotes": [{"symbolId": option_id("2026-11-20", 100, "C"), "bidPrice": 1.5}]}]
        got = []
        task = asyncio.create_task(stream_for(src).run("option", [option_id("2026-11-20", 100, "C")], got.extend))
        await until(lambda: got)
        await stop(task)
    body = json.loads(next(r for r in fake.requests if r.url.path == "/v1/markets/quotes/options").content)
    assert body == {"optionIds": [option_id("2026-11-20", 100, "C")], "stream": True, "mode": "WebSocket"}
    assert got[0]["bidPrice"] == 1.5


async def test_refused_token_is_a_handshake_error(qt, caplog):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    caplog.set_level(logging.INFO, "optionlens.live.stream")
    async with FakeStreamServer(fake) as srv:
        srv.refuse = True
        with pytest.raises(HandshakeError, match="did not accept"):
            await stream_for(src).run("equity", [1000], lambda q: None)
    assert src.tokens.state == "connected"          # a refused stream token is not a REST auth failure
    assert "handshake refused" in caplog.text and fake.access not in caplog.text


async def test_a_drop_raises_and_renewal_returns_before_the_token_expires(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.drop_after = 1
        with pytest.raises(StreamDropped):
            await asyncio.wait_for(stream_for(src).run("equity", [1000], lambda q: None), 5)
        srv.drop_after = None
        fake.expires_in = 61                                             # renew margin is 60 s: return within ~1 s
        src.tokens._access = None
        await asyncio.wait_for(stream_for(src).run("equity", [1000], lambda q: None), 5)


async def test_an_expired_access_token_is_refreshed_by_the_port_request(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        fake.access = "expired-server-side"           # Questrade no longer knows the engine's access token
        fake.valid.add("rotated-1")
        got = []
        task = asyncio.create_task(stream_for(src).run("equity", [1000], got.extend))
        await until(lambda: got)
        await stop(task)
    assert got and srv.tokens == ["access-2"]         # 401 -> refreshed -> the new token opened the stream


def test_scrub_masks_secret_fields_and_the_token_and_truncates():
    raw = json.dumps({"quotes": [{"symbolId": 1, "bidPrice": 1.0}], "sessionToken": "abc", "note": "tok-XYZ"})
    out = scrub(raw, "tok-XYZ")
    assert '"sessionToken":"***"' in out and "tok-XYZ" not in out and '"bidPrice":1.0' in out
    long = json.dumps({"quotes": [{"symbolId": i, "bidPrice": i} for i in range(100)]})
    assert len(scrub(long, None)) == stream_mod.LOG_CHARS + 1 and scrub(b"not json", None) == "not json"


# ----------------------------------------------------------------------------- the driver
async def test_driver_streams_merges_partial_messages_and_reconnects_after_a_drop(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    published = []
    async with FakeStreamServer(fake) as srv:
        srv.messages = [{"quotes": [fake.equity_quote()]}, {"quotes": [{"symbolId": 1000, "lastTradePrice": 101.0}]}]
        srv.drop_after = 2
        d = driver(src, published)
        d.set_wanted(Wanted(frozenset({"XYZ"})))
        task = asyncio.create_task(d.run())
        try:
            await until(lambda: len(srv.tokens) >= 2 and len(published) >= 3)
        finally:
            await stop(task)
    first, second = published[0][0]["XYZ"], published[1][0]["XYZ"]
    assert first["price"] == 100.2 and first["bid"] == 99.9
    assert second["price"] == 101.0 and second["bid"] == 99.9             # partial message merged per symbolId
    assert second["change"] == pytest.approx(3.0)                         # vs prevDayClosePrice 98


async def test_token_refresh_reconnects_with_a_new_port_request_and_token(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        d = driver(src)
        d.set_wanted(Wanted(frozenset({"XYZ"})))
        task = asyncio.create_task(d.run())
        try:
            await until(lambda: d.diagnostics()["streams"] and d.diagnostics()["streams"][0]["open"])
            first = fake.access
            src.tokens._access = None
            await src.tokens.access()                  # refreshed elsewhere (e.g. a sweep's call): a new token
            assert fake.access != first
            await until(lambda: srv.tokens[-1] == fake.access and d.diagnostics()["streams"][0]["open"])
            assert d.diagnostics()["streams"][0]["connects"] == 2 and d.fallback() is None
        finally:
            await stop(task)
    assert srv.tokens == [first, fake.access] and port_requests(fake) == 2


async def test_handshake_failure_falls_back_to_polling_and_retries_streaming_later(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    clock = Shift()
    async with FakeStreamServer(fake) as srv:
        srv.refuse = True
        d = driver(src, clock=clock)
        d.set_wanted(Wanted(frozenset({"XYZ"}), {C1: "XYZ"}))
        task = asyncio.create_task(d.run())
        try:
            await until(lambda: d.rounds >= 1)
            h = d.health()
            assert h["mode"] == "poll" and h["fallback"] == "handshake failed" and not d.streaming()
            diag = d.diagnostics()
            assert diag["setting"] == "stream" and diag["fallback"] == "handshake failed" and diag["retry_at"]
            tried = len(srv.tokens)
            srv.refuse = False
            clock.offset = qlive.STREAM_RETRY_S + 1                       # 5 min later: streaming again
            await until(lambda: len(srv.tokens) > tried and d.streaming() and d.health()["mode"] == "stream")
            await until(lambda: len(d.diagnostics()["streams"]) == 2
                        and all(s["open"] for s in d.diagnostics()["streams"]))
            assert d.fallback() is None
        finally:
            await stop(task)


async def test_a_silent_stream_in_market_hours_falls_back_to_polling(qt, monkeypatch):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    monkeypatch.setattr(qlive, "STREAM_SILENT_S", 0.3)
    async with FakeStreamServer(fake) as srv:
        srv.messages = []                                                # {"success": true}, then nothing
        quiet = driver(src, regular=False)                               # outside the regular session: tolerated
        quiet.set_wanted(Wanted(frozenset({"XYZ"})))
        task = asyncio.create_task(quiet.run())
        await asyncio.sleep(0.8)
        assert quiet.streaming() and quiet.fallback() is None
        await stop(task)
        d = driver(src, regular=True)
        d.set_wanted(Wanted(frozenset({"XYZ"})))
        task = asyncio.create_task(d.run())
        try:
            await until(lambda: d.rounds >= 1)
        finally:
            await stop(task)
    assert d.fallback().startswith("no messages within") and d.health()["mode"] == "poll"


async def test_a_drop_storm_falls_back_to_polling(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.drop_after = 0                                               # accepted, then closed at once
        d = driver(src)
        d.set_wanted(Wanted(frozenset({"XYZ"})))
        task = asyncio.create_task(d.run())
        try:
            await until(lambda: d.rounds >= 1)
        finally:
            await stop(task)
    assert len(srv.tokens) == qlive.DROP_LIMIT                           # reconnected after drops 1 and 2
    assert d.fallback() == "3 drops in 5 min" and d.health()["mode"] == "poll"


async def test_a_changed_set_reopens_only_the_stream_whose_ids_changed(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.tick_s = 0.05
        d = driver(src)
        d.set_wanted(Wanted(frozenset({"XYZ"}), {C1: "XYZ"}))
        task = asyncio.create_task(d.run())
        streams = lambda: {s["kind"]: s for s in d.diagnostics()["streams"]}       # noqa: E731
        try:
            await until(lambda: len(streams()) == 2 and all(s["open"] for s in streams().values()))
            d.set_wanted(Wanted(frozenset({"XYZ"}), {C1: "XYZ", C2: "XYZ"}))
            await until(lambda: len(srv.tokens) == 3 and len(streams()) == 2
                        and all(s["open"] for s in streams().values()))
            assert streams()["equity"]["connects"] == 1 and streams()["option"]["ids"] == 2
            assert d.diagnostics()["messages_per_s"] > 0 and d.diagnostics()["last_message_at"]
        finally:
            await stop(task)
    assert sum(1 for p in srv.paths if p.startswith("/v1/markets/quotes?")) == 1     # the equity stream stayed open


async def test_switching_the_mode_at_runtime_both_ways(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.tick_s = 0.05
        d = driver(src, mode="poll")
        d.set_wanted(Wanted(frozenset({"XYZ"})))
        task = asyncio.create_task(d.run())
        try:
            await until(lambda: d.rounds >= 1)
            assert srv.tokens == [] and d.health()["mode"] == "poll" and d.health()["fallback"] is None
            d.set_mode("stream")                                         # no restart: streams open at once
            await until(lambda: d.diagnostics()["streams"] and d.diagnostics()["streams"][0]["open"])
            assert d.health()["mode"] == "stream" and d.health()["interval_s"] is None
            rounds = d.rounds
            d.set_mode("poll")                                           # streams closed, polling again
            await until(lambda: srv.closed == 1 and d.rounds > rounds)
            assert d.diagnostics()["streams"] == [] and d.health()["mode"] == "poll"
            with pytest.raises(ValueError):
                d.set_mode("websocket")
        finally:
            await stop(task)
    assert len(srv.tokens) == 1
