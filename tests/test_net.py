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
    assert s.sweep_interval_min == 30 and s.cboe_rps == 3.0 and s.data_dir == tmp_path
