"""Questrade Level 1 streaming (live/stream.py, live/questrade.py stream mode) against a local WebSocket server that
plays Questrade's documented handshake: the port request, the access token as the first message, {"success": true},
then quote messages; plus drops, a refused token and the renewal before the access token expires."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from websockets.asyncio.server import serve

from optionlens.analytics.calendar import NY
from optionlens.live import questrade as qlive
from optionlens.live.stream import QuoteStream, StreamError
from optionlens.live.subscriptions import Wanted
from optionlens.sources.questrade import source as source_mod
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade, option_id

C1 = "XYZ261120C00100000"


class FakeStreamServer:
    """Accepts sockets on 127.0.0.1; `script(ws, path)` decides what each connection does after the token check."""

    def __init__(self, fake: FakeQuestrade):
        self.fake = fake
        self.tokens: list[str] = []
        self.paths: list[str] = []
        self.user_agents: list[str | None] = []
        self.refuse = False
        self.drop_after: int | None = None       # close the socket after this many quote messages
        self.messages: list[dict] = [{"quotes": [{"symbolId": 1000, "lastTradePrice": 100.5}]}]

    async def handler(self, ws):
        self.paths.append(ws.request.path)
        self.user_agents.append(ws.request.headers.get("User-Agent"))
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
        await ws.wait_closed()

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
    return fake, src


def stream_for(src) -> QuoteStream:
    """A QuoteStream whose socket goes to 127.0.0.1 (the API server's host is a questrade.com name) over ws://."""
    from websockets.asyncio.client import connect

    def local(url, **kw):
        host = url.split("://", 1)[1].split(":", 1)[0]
        return connect(url.replace(host, "127.0.0.1", 1), **kw)
    return QuoteStream(src.api, src.tokens, connect=local, scheme="ws")


async def test_handshake_token_first_then_quotes(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        got, opened = [], []
        s = stream_for(src)
        task = asyncio.create_task(s.run("equity", [1000], got.extend, lambda: opened.append(1)))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if got:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert srv.tokens == [fake.access]                                   # the bare token, no "Bearer"
    assert opened == [1] and got == [{"symbolId": 1000, "lastTradePrice": 100.5}]
    assert srv.paths == ["/v1/markets/quotes?ids=1000&stream=true&mode=WebSocket"]
    assert srv.user_agents == ["OptionLens"]
    port_req = next(r for r in fake.requests if r.url.params.get("stream") == "true")
    assert port_req.url.params["mode"] == "WebSocket" and port_req.url.params["ids"] == "1000"


async def test_option_stream_port_request_is_a_post_with_option_ids(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.messages = [{"optionQuotes": [{"symbolId": option_id("2026-11-20", 100, "C"), "bidPrice": 1.5}]}]
        got = []
        task = asyncio.create_task(stream_for(src).run("option", [option_id("2026-11-20", 100, "C")], got.extend))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if got:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    body = json.loads(next(r for r in fake.requests if r.url.path == "/v1/markets/quotes/options").content)
    assert body == {"optionIds": [option_id("2026-11-20", 100, "C")], "stream": True, "mode": "WebSocket"}
    assert got[0]["bidPrice"] == 1.5


async def test_refused_token_raises(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.refuse = True
        with pytest.raises(StreamError, match="did not accept"):
            await stream_for(src).run("equity", [1000], lambda q: None)
    assert src.tokens.state == "connected"          # a refused stream token is not a REST auth failure


async def test_a_drop_raises_and_renewal_returns_before_the_token_expires(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.drop_after = 1
        with pytest.raises(StreamError):
            await asyncio.wait_for(stream_for(src).run("equity", [1000], lambda q: None), 5)
        srv.drop_after = None
        fake.expires_in = 61                                             # renew margin is 60 s: return within ~1 s
        src.tokens._access = None
        await asyncio.wait_for(stream_for(src).run("equity", [1000], lambda q: None), 5)


async def test_driver_streams_merges_partial_messages_and_reconnects_after_a_drop(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    published = []
    async with FakeStreamServer(fake) as srv:
        srv.messages = [{"quotes": [fake.equity_quote()]}, {"quotes": [{"symbolId": 1000, "lastTradePrice": 101.0}]}]
        srv.drop_after = 2
        d = qlive.QuestradeLive(src, lambda s, q, o: published.append(dict(q)), mode="stream",
                                sleep=lambda s: asyncio.sleep(0.01), stream=stream_for(src))
        d.set_wanted(Wanted(frozenset({"XYZ"})))
        task = asyncio.create_task(d.run())
        try:
            for _ in range(300):
                await asyncio.sleep(0.01)
                if len(srv.tokens) >= 2 and len(published) >= 3:
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(srv.tokens) >= 2                                          # reconnected after the drop
    first, second = published[0]["XYZ"], published[1]["XYZ"]
    assert first["price"] == 100.2 and first["bid"] == 99.9
    assert second["price"] == 101.0 and second["bid"] == 99.9             # partial message merged per symbolId
    assert second["change"] == pytest.approx(3.0)                         # vs prevDayClosePrice 98


async def test_driver_falls_back_to_polling_when_streams_keep_failing(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        srv.refuse = True
        d = qlive.QuestradeLive(src, lambda *a: None, mode="stream", sleep=lambda s: asyncio.sleep(0),
                                stream=stream_for(src))
        d.set_wanted(Wanted(frozenset({"XYZ"}), {C1: "XYZ"}))
        task = asyncio.create_task(d.run())
        try:
            for _ in range(300):
                await asyncio.sleep(0.01)
                if not d.streaming() and d.rounds:
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not d.streaming() and d.health()["mode"] == "poll" and d.rounds >= 1
    assert d._poll_until > 0


async def test_an_expired_access_token_is_refreshed_by_the_port_request(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    async with FakeStreamServer(fake) as srv:
        fake.access = "expired-server-side"           # Questrade no longer knows the engine's access token
        fake.valid.add("rotated-1")
        got = []
        task = asyncio.create_task(stream_for(src).run("equity", [1000], got.extend))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if got:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert got and srv.tokens == ["access-2"]         # 401 -> refreshed -> the new token opened the stream
