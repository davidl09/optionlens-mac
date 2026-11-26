"""Rate limiter and settings validation (offline)."""
from __future__ import annotations

import asyncio

import pytest

from optionlens import config, net


async def test_token_bucket_sub_one_rate_yields_tokens():
    bucket = net._TokenBucket(0.5)
    assert bucket.capacity == 1.0
    await asyncio.wait_for(bucket.acquire(), 1.0)             # used to wait forever: capacity was 0.5
    bucket.updated -= 2.0                                     # 2 s later at 0.5 tokens/s -> one more token
    await asyncio.wait_for(bucket.acquire(), 1.0)
    with pytest.raises(ValueError):
        net._TokenBucket(0)


@pytest.mark.parametrize("name,value,msg", [
    ("SWEEP_INTERVAL_MIN", "0", "OPTIONLENS_SWEEP_INTERVAL_MIN=0 must be > 0"),
    ("CBOE_RPS", "-1", "OPTIONLENS_CBOE_RPS=-1.0 must be > 0"),
    ("CBOE_CONCURRENCY", "0", "OPTIONLENS_CBOE_CONCURRENCY=0 must be > 0"),
    ("SWEEP_INTERVAL_MIN", "abc", "OPTIONLENS_SWEEP_INTERVAL_MIN='abc' is not a valid int"),
])
def test_settings_fail_fast(monkeypatch, tmp_path, name, value, msg):
    monkeypatch.setenv("OPTIONLENS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(f"OPTIONLENS_{name}", value)
    with pytest.raises(ValueError, match=msg.replace("(", r"\(")):
        config.load_settings()


def test_default_settings_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("OPTIONLENS_DATA_DIR", str(tmp_path))
    for name in ("SWEEP_INTERVAL_MIN", "CBOE_RPS"):
        monkeypatch.delenv(f"OPTIONLENS_{name}", raising=False)
    s = config.load_settings()
    assert s.sweep_interval_min == 60 and s.cboe_rps == 1.5 and s.data_dir == tmp_path


from optionlens import net as _net_mod

_REAL_GET = _net_mod.get  # captured at import: session fixtures in tests/api monkeypatch net.get offline


def test_cdn_block_page_suspends_host_and_is_not_treated_as_unlisted(monkeypatch):
    """A Cloudflare 403 HTML page must suspend the host (fail fast, no retries) and carry blocked=True."""
    import asyncio
    import httpx
    import pytest
    from optionlens import net

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(403, headers={"content-type": "text/html"}, text="<html>blocked</html>")

    net._blocks.clear()
    net._blocks_loaded = True
    monkeypatch.setattr(net, "_save_blocks", lambda: None)

    async def run():
        n = net._net()
        await n.client.aclose()
        n.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(net.FetchError) as e1:
            await _REAL_GET("https://cdn.cboe.com/api/global/delayed_quotes/options/AAPL.json")
        with pytest.raises(net.FetchError) as e2:  # suspended: no second request goes out
            await _REAL_GET("https://cdn.cboe.com/api/global/delayed_quotes/options/MSFT.json")
        await net.aclose()
        return e1.value, e2.value

    try:
        e1, e2 = asyncio.run(run())
        assert e1.blocked and e1.status == 403 and e2.blocked
        assert len(calls) == 1
        assert net.blocked_until("cdn-api.cboe.com") is not None
        assert net.blocked_until("api.nasdaq.com") is None
    finally:
        net._blocks.clear()


def test_json_403_is_not_a_block(monkeypatch):
    import asyncio
    import httpx
    import pytest
    from optionlens import net

    net._blocks.clear()
    net._blocks_loaded = True
    monkeypatch.setattr(net, "_save_blocks", lambda: None)

    async def run():
        n = net._net()
        await n.client.aclose()
        n.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(403, json={"error": "not found"})))
        with pytest.raises(net.FetchError) as e:
            await _REAL_GET("https://cdn.cboe.com/api/global/delayed_quotes/options/ZZZZ.json")
        await net.aclose()
        return e.value

    err = asyncio.run(run())
    assert err.status == 403 and not err.blocked and net.blocked_until("cdn.cboe.com") is None


def test_concurrent_block_pages_escalate_once_and_state_survives_restart(tmp_path, monkeypatch):
    from optionlens import config, net

    monkeypatch.setenv("OPTIONLENS_DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    net._blocks.clear()
    net._blocks_loaded = True
    try:
        net._register_block("cdn.cboe.com")
        net._register_block("cdn-api.cboe.com")          # second in-flight block page: no doubling
        until, pause = net._blocks["cboe.com"]
        assert pause == net.BLOCK_PAUSE_S
        assert (tmp_path / "net_blocks.json").exists()
        net._blocks.clear()                               # simulate a restart
        net._blocks_loaded = False
        assert net.blocked_until("cdn.cboe.com") == until
    finally:
        net._blocks.clear()
        net._blocks_loaded = False
        config.get_settings.cache_clear()


def test_post_json_headers_and_a_429_budget(monkeypatch):
    """POST bodies and per-request headers reach the server; max_throttles=0 gives up at the first 429 (a source that
    throttles an honest client is left for the fallback instead of waited out)."""
    import asyncio
    import json as _json
    import httpx
    from optionlens import net

    seen = []

    def handler(request):
        seen.append((request.method, request.headers["user-agent"], request.content))
        if request.url.path == "/throttled":
            return httpx.Response(429, headers={"content-type": "text/plain"}, text="Too Many Requests")
        return httpx.Response(200, json={"ok": True})

    net._blocks.clear()
    net._blocks_loaded = True
    monkeypatch.setattr(net, "_save_blocks", lambda: None)

    async def run():
        n = net._net()
        await n.client.aclose()
        n.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": "default"})
        got = await net.post_json("https://example.org/graphql", {"q": 1}, headers={"User-Agent": "OptionLens/x"})
        with pytest.raises(net.FetchError) as e:
            await _REAL_GET("https://example.org/throttled", max_throttles=0)
        await net.aclose()
        return got, e.value

    got, err = asyncio.run(run())
    assert got == {"ok": True} and seen[0][:2] == ("POST", "OptionLens/x") and _json.loads(seen[0][2]) == {"q": 1}
    assert err.status == 429 and "Too Many Requests" in str(err) and len(seen) == 2   # one try, no waiting
