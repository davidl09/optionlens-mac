"""GET /api/symbol/{sym}/bars?interval=… and POST /api/ingest/intraday-bars over HTTP: validation, the daily
behaviour unchanged, Questrade candles cached between requests, a bridge push filling the missing spans, and the
IntradayBars shape of types.ts."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from optionlens.analytics import intraday
from optionlens.analytics.calendar import NY, is_early_close, next_trading_day
from optionlens.config import get_settings
from optionlens.services import intraday as service
from optionlens.sources import chain_source
from optionlens.sources.questrade.source import QuestradeSource

from ..questrade_fake import FakeQuestrade
from .tsschema import load_types, mismatches


@pytest.fixture(autouse=True)
def _fresh_locks():
    service._locks.clear()


def test_validation_and_daily_unchanged(client):
    daily = client.get("/api/symbol/AAPL/bars?range=1y")
    assert daily.status_code == 200 and isinstance(daily.json(), list) and "date" in daily.json()[0]
    assert client.get("/api/symbol/AAPL/bars?range=1d").status_code == 422           # not a daily range
    assert client.get("/api/symbol/AAPL/bars?interval=1m&range=1y").status_code == 422
    assert client.get("/api/symbol/AAPL/bars?interval=1m&range=5d").status_code == 422   # not offered
    assert client.get("/api/symbol/AAPL/bars?interval=2m&range=1d").status_code == 422


def test_bridge_push_fills_the_missing_spans(client):
    schema = load_types().parse("IntradayBars")
    r = client.get("/api/symbol/DELL/bars?interval=1h&range=3m")
    body = r.json()
    assert r.status_code == 200 and mismatches(body, schema) == [] and body["bars"] == []
    assert body["missing"] and body["sources"] == []
    day = next_trading_day(datetime.fromisoformat(body["missing"][0]["start"]).astimezone(NY).date())
    while is_early_close(day):
        day = next_trading_day(day)
    bars = [{"ts": datetime.combine(day, time(10 + h), NY).isoformat(), "open": 50, "high": 51, "low": 49, "close": 50.5,
             "volume": 10} for h in range(7)]                                 # 10:00..16:00 ET of a full session
    push = {"source": "ibkr", "symbol": "DELL", "interval": "1h", "delayed": None, "spans": body["missing"], "bars": bars}
    assert client.post("/api/ingest/intraday-bars", json={**push, "interval": "2h"}).status_code == 422
    assert client.post("/api/ingest/intraday-bars", json={**push, "spans": []}).status_code == 422
    r = client.post("/api/ingest/intraday-bars", json=push)
    assert r.status_code == 200 and r.json() == {"stored": 7, "symbol": "DELL"}
    body = client.get("/api/symbol/DELL/bars?interval=1h&range=3m").json()
    assert mismatches(body, schema) == [] and body["sources"] == ["ibkr"] and body["delayed"] is None
    assert [b["close"] for b in body["bars"]] == [50.5] * 6                       # 16:00 is after the close
    assert len(client.get("/api/symbol/DELL/bars?interval=1h&range=3m&extended=1").json()["bars"]) == 7
    left = [datetime.fromisoformat(m["end"]) - datetime.fromisoformat(m["start"]) for m in body["missing"]]
    assert all(d <= intraday.RECENT + timedelta(minutes=1) for d in left)       # only the unsettled tail


def test_questrade_candles_are_cached(client, monkeypatch, tmp_path):
    fake = FakeQuestrade()
    fake.listed["SPY"] = 1000
    src = QuestradeSource(tmp_path, get_settings().questrade_expiries, get_settings().questrade_moneyness, fake.http)
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: src)
    assert client.post("/api/sources/questrade/token", json={"refresh_token": "pasted-token"}).status_code == 200
    calls = lambda: [r for r in fake.requests if r.url.path.startswith("/v1/markets/candles/")]
    body = client.get("/api/symbol/SPY/bars?interval=15m&range=1m").json()
    assert mismatches(body, load_types().parse("IntradayBars")) == []
    assert body["sources"] == ["questrade"] and body["missing"] == [] and body["has_extended"] is True
    first = len(calls())
    assert 1 <= first <= 2
    client.get("/api/symbol/SPY/bars?interval=15m&range=1m&extended=1")
    again = calls()[first:]
    assert len(again) <= 1                                                     # at most the recent tail
    assert all(datetime.fromisoformat(r.url.params["startTime"]) >= datetime.now(timezone.utc) - intraday.RECENT
               - timedelta(minutes=1) for r in again)
