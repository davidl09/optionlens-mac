"""Questrade error classes (errors.py: Questrade's documented error table) and what follows from them: an auth /
permission answer (403 code 1016) stops the source at once — no per-symbol retries, persisted needs_attention, no
further sweeps or refreshes until a new token is pasted — with grouped failure reasons; a new token is checked with
one market-data call."""
from __future__ import annotations

import httpx
import pytest

from optionlens.jobs import chains, source_health
from optionlens.sources import chain_source
from optionlens.sources.questrade import errors
from optionlens.sources.questrade.auth import TokenManager, TokenStore
from optionlens.sources.questrade.errors import QuestradeError
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade
from .test_questrade_client import FakeTime, connected_client, gate

M1016 = "Your Questrade app lacks the 'Retrieve delayed and real-time market data' permission. In Questrade's " \
        "API Centre enable it for your personal app, generate a new manual token, and paste it here."


def test_classes_follow_the_documented_table():
    assert errors.classify(403, 1016) == "auth" and errors.classify(401, 1017) == "auth"
    assert errors.classify(401, 1014) == "auth" and errors.classify(400, 1015) == "auth"
    assert errors.classify(403, None) == "auth"
    assert errors.classify(429, 1006) == "rate"
    assert all(errors.classify(st, c) == "transient" for st, c in ((500, 1007), (500, 1008), (500, 1009),
                                                                    (502, 1010), (503, 1011), (500, 1021), (None, None)))
    assert errors.classify(404, 1019) == "other" and errors.classify(400, 1002) == "other"
    assert errors.reason(403, 1016) == "403 code 1016 (missing market-data permission)"
    assert errors.reason(404, None, "Not found.") == "404 (Not found)"
    assert errors.attention(403, 1016, "Request is out of allowed OAuth scopes") == M1016
    assert "code 1017" in errors.attention(401, 1017, None) and "new manual token" in errors.attention(401, 1017, None)
    other = errors.attention(403, 1099, "Nope")
    assert "HTTP 403 code 1099: Nope" in other and "API Centre" in other
    e = QuestradeError("u", 403, 1016, "Request is out of allowed OAuth scopes")
    assert e.kind == "auth" and e.attention == M1016 and e.status == 403 and "1016" in str(e)
    assert QuestradeError("u", 404, 1019, "Symbol not found.").attention is None


async def test_403_1016_flags_the_token_and_stops_every_further_call(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.scope_denied = True
    with pytest.raises(QuestradeError) as e:
        await c.get("symbols/search", {"prefix": "XYZ"})
    assert e.value.kind == "auth" and e.value.code == 1016
    assert c.tokens.state == "needs_attention" and c.tokens.unusable() == M1016
    n = len(fake.requests)
    with pytest.raises(QuestradeError) as again:
        await c.get("symbols/search", {"prefix": "ABC"})
    assert again.value.kind == "auth" and len(fake.requests) == n                    # no call made
    # persisted: a restart does not try again
    tm = TokenManager(TokenStore(tmp_path / "secrets" / "questrade.json"), fake.http, clock=ft)
    assert tm.state == "needs_attention" and tm.unusable() == M1016
    assert not await tm.keepalive(0)                                                 # no logins either
    # a new token clears it
    fake.scope_denied = False
    fake.valid.add("new-token")
    await tm.connect("new-token")
    assert tm.state == "connected" and tm.unusable() is None


async def test_401_after_the_refresh_needs_attention(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.queue["/v1/symbols/1000"] = [httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})] * 2
    with pytest.raises(QuestradeError) as e:
        await c.get("symbols/1000")
    assert e.value.kind == "auth" and c.tokens.state == "needs_attention" and "code 1017" in c.tokens.detail
    assert fake.logins == ["pasted-token", "rotated-1"]                              # exactly one refresh


async def test_transient_and_refused_requests_do_not_stop_the_source(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    c = await connected_client(tmp_path, fake, ft)
    fake.queue["/v1/symbols/1000"] = [httpx.Response(404, json={"code": 1019, "message": "Symbol not found."})]
    with pytest.raises(QuestradeError) as e:
        await c.get("symbols/1000")
    assert e.value.kind == "other" and e.value.reason == "404 code 1019 (symbol not found)"
    fake.queue["/v1/symbols/1000"] = [httpx.Response(503, json={"code": 1011, "message": "timeout"})] * 4
    with pytest.raises(QuestradeError) as e:
        await c.get("symbols/1000")
    assert e.value.kind == "transient" and c.tokens.state == "connected"


@pytest.fixture
async def source(tmp_path):
    fake, ft = FakeQuestrade(), FakeTime()
    src = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=fake.http, gate=gate(ft))
    src.api.sleep = ft.sleep
    await src.tokens.connect("pasted-token")
    return fake, src


async def test_a_sweep_stops_at_the_first_permission_error(source, monkeypatch):
    """The live failure: 1,141 symbols, every call 403 code 1016. Now: the first answer stops the sweep (at most the
    calls already in flight), nothing is retried, and the detail and status group the reason."""
    fake, src = source
    fake.scope_denied = True
    syms = [f"S{i:04d}" for i in range(1141)]
    got, detail = await chains._sweep_source(src, syms, final=False)
    searches = fake.path_count("/v1/symbols/search")
    assert got == [] and searches <= chains.SWEEP_CONCURRENCY
    assert f"{searches} failed" in detail and "403 code 1016 (missing market-data permission)" in detail
    assert f"stopped, {1141 - searches} not asked: {M1016}" in detail
    assert source_health.top_failures("questrade") == [
        {"reason": "403 code 1016 (missing market-data permission)", "count": searches}]
    assert src.unavailable() == M1016
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: src)
    assert await chains.sweep(syms) == f"questrade: {M1016}"                        # no further sweeps
    assert fake.path_count("/v1/symbols/search") == searches


async def test_verify_reports_the_token_check(source):
    fake, src = source
    ok = await src.verify()
    assert ok["ok"] is True and ok["message"] == "Token OK: market data permitted"
    fake.scope_denied = True
    bad = await src.verify()
    assert bad == {**bad, "ok": False, "message": M1016} and src.status()["verification"]["ok"] is False
    assert src.tokens.state == "needs_attention"


async def test_the_short_reason_is_kept_with_needs_attention(source, tmp_path):
    fake, src = source
    fake.scope_denied = True
    with pytest.raises(QuestradeError):
        await src.symbol_id("XYZ")
    assert src.problem() == "403 code 1016 (missing market-data permission)"
    again = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=fake.http)
    assert again.problem() == "403 code 1016 (missing market-data permission)" and again.unavailable() == M1016
