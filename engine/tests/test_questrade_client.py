"""Questrade pacing (ratelimit.RateGate: documented limits, response headers, 429) and the REST client (401 ->
refresh -> retry, 429 / 5xx retries, errors)."""
from __future__ import annotations

import httpx
import pytest

from optionlens.net import FetchError
from optionlens.sources.questrade import ratelimit
from optionlens.sources.questrade.auth import TokenManager, TokenStore
from optionlens.sources.questrade.client import QuestradeClient
from optionlens.sources.questrade.ratelimit import RateGate

from .questrade_fake import FakeQuestrade


class FakeTime:
    """A clock and an async sleep that advances it (records each sleep)."""

    def __init__(self, t: float = 1_800_000_000.0):
        self.t, self.slept = t, []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(round(s, 6))
        self.t += s


def gate(ft: FakeTime, **kw) -> RateGate:
    return RateGate(clock=ft, sleep=ft.sleep, **kw)


# ----------------------------------------------------------------------------- rate gate
def test_documented_limits_and_share():
    g = RateGate()
    assert (ratelimit.PER_SECOND, ratelimit.PER_HOUR, ratelimit.SHARE) == (20, 15_000, 0.70)
    assert g.hour_budget == 10_500 and g.interval == pytest.approx(1 / 14)


async def test_calls_are_spaced_to_70_percent_of_the_per_second_limit():
    ft = FakeTime()
    g = gate(ft)
    for _ in range(15):
        await g.acquire()
    assert ft.t - 1_800_000_000.0 == pytest.approx(14 / 14)                          # 15 calls take 1 s: 14 rps


async def test_rolling_hour_budget():
    ft = FakeTime()
    g = gate(ft, per_second=1000, per_hour=10)                                       # budget 7 per hour
    for _ in range(7):
        await g.acquire()
    start = ft.t
    await g.acquire()
    assert ft.t >= start + 3600 - 1 and g.stats()["calls_last_hour"] <= 7


async def test_headers_pause_at_70_percent_of_the_current_limit():
    ft = FakeTime()
    g = gate(ft)
    g.observe({"X-RateLimit-Remaining": "4501", "X-RateLimit-Reset": str(ft.t + 1200)}, 200)   # hourly: 69.99% used
    await g.acquire()
    assert ft.slept == []
    g.observe({"X-RateLimit-Remaining": "4500", "X-RateLimit-Reset": str(ft.t + 1200)}, 200)   # 70% used
    reset = ft.t + 1200
    await g.acquire()
    assert ft.t == pytest.approx(reset)
    s = g.stats()
    assert s["remaining"] == 4500 and s["reset_at"] is not None and s["paused_until"] is None
    g.observe({"X-RateLimit-Remaining": "6", "X-RateLimit-Reset": str(ft.t + 1)}, 200)   # per-second window: 14/20 used
    before = ft.t
    await g.acquire()
    assert ft.t == pytest.approx(before + 1)
    g.observe({"X-RateLimit-Remaining": "nope"}, 200)                                 # malformed: ignored
    assert g.stats()["remaining"] == 6


async def test_429_pauses_until_reset_or_backs_off():
    ft = FakeTime()
    g = gate(ft)
    g.observe({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(ft.t + 30)}, 429)
    assert g.stats()["paused_until"] is not None
    await g.acquire()
    assert ft.slept == [30.0]
    g.observe({}, 429)
    await g.acquire()
    assert ft.slept[-1] == pytest.approx(ratelimit.BACKOFF_429_S)


# ----------------------------------------------------------------------------- client
async def connected_client(tmp_path, fake: FakeQuestrade, ft: FakeTime) -> QuestradeClient:
    tm = TokenManager(TokenStore(tmp_path / "secrets" / "questrade.json"), fake.http, ft)
    await tm.connect("pasted-token")
    return QuestradeClient(tm, gate(ft), fake.http, sleep=ft.sleep)


async def test_401_refreshes_once_and_retries(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.access = "revoked-elsewhere"                                                # server forgot our access token
    fake.valid.add("rotated-1")
    body = await c.get("symbols/search", {"prefix": "XYZ"})
    assert body["symbols"][0]["symbolId"] == 1000
    assert fake.logins == ["pasted-token", "rotated-1"]
    calls = [r for r in fake.requests if r.url.path == "/v1/symbols/search"]
    assert [r.headers["Authorization"] for r in calls] == ["Bearer access-1", "Bearer access-2"]
    assert calls[-1].headers["User-Agent"] == "OptionLens"


async def test_401_twice_is_an_error(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.queue["/v1/symbols/1000"] = [httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})] * 2
    with pytest.raises(FetchError, match="1017"):
        await c.get("symbols/1000")


async def test_429_waits_for_the_reset_then_retries(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.queue["/v1/markets/quotes"] = [httpx.Response(
        429, json={"code": 1006, "message": "Rate limit exceeded."},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(ft.t) + 20)})]
    t0 = ft.t
    body = await c.get("markets/quotes", {"ids": "1000"})
    assert body["quotes"][0]["symbolId"] == 1000 and ft.t >= t0 + 19


async def test_5xx_retries_with_backoff_then_fails(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.queue["/v1/symbols/1000"] = [httpx.Response(503)]
    assert (await c.get("symbols/1000"))["symbols"][0]["hasOptions"] is True
    fake.queue["/v1/symbols/1000"] = [httpx.Response(500, json={"code": 1008, "message": "technical error"})] * 4
    with pytest.raises(FetchError) as e:
        await c.get("symbols/1000")
    assert e.value.status == 500 and "1008" in str(e.value)


async def test_client_errors_are_not_retried_and_token_problems_are_fetch_errors(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.queue["/v1/symbols/1000"] = [httpx.Response(404, json={"code": 1019, "message": "Symbol not found."})]
    with pytest.raises(FetchError, match="Symbol not found"):
        await c.get("symbols/1000")
    assert fake.path_count("/v1/symbols/1000") == 1
    c.tokens.disconnect()
    with pytest.raises(FetchError, match="not connected"):
        await c.get("symbols/1000")
