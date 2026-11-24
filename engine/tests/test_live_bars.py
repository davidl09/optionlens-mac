"""Live 1m bars: live quotes -> 1m OHLCV (live/bars.py), the hub handing them over, and the intraday service serving
them after the source's (lagging) candles: source bars win, 5m/15m/1h aggregated from 1m, live-only spans re-asked."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from optionlens import db, live
from optionlens.analytics import intraday
from optionlens.analytics.calendar import NY
from optionlens.live.bars import MinuteBars
from optionlens.live.hub import LiveHub
from optionlens.models import Bar, IntradayBar
from optionlens.repo import bars as bars_repo
from optionlens.repo import intraday as repo
from optionlens.repo import symbols as symbols_repo
from optionlens.services import intraday as service
from optionlens.sources import chain_source
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


def at(h, m, s=0.0, d=17) -> float:
    return ny(2026, 11, d, h, m).timestamp() + s


def bar(h, m, o, hi, lo, c, v, d=17) -> IntradayBar:
    return IntradayBar((ny(2026, 11, d, h, 0) + timedelta(minutes=m)).astimezone(timezone.utc), o, hi, lo, c, v)


# ----------------------------------------------------------------------------- ticks -> 1m bars (pure)
def test_ticks_become_1m_ohlcv_with_volume_deltas_and_no_bar_without_a_trade():
    mb = MinuteBars()
    mb.tick("XYZ", 100.0, 1000, False, at(10, 0, 5))        # the first quote only anchors
    assert mb.pending("XYZ") == []
    mb.tick("XYZ", 100.5, 1100, False, at(10, 0, 20))
    mb.tick("XYZ", 99.8, 1150, False, at(10, 0, 40))
    mb.tick("XYZ", 100.1, 1150, False, at(10, 0, 50))       # day volume unchanged: no trade (a quote change only)
    mb.tick("XYZ", 100.2, 1300, False, at(10, 1, 0))        # 10:01:00 ET: the next minute
    mb.tick("XYZ", 101.0, 1400, False, at(10, 3, 59.9))     # nothing traded at 10:02: no bar
    assert mb.pending("XYZ") == [bar(10, 0, 100.5, 100.5, 99.8, 99.8, 150), bar(10, 1, 100.2, 100.2, 100.2, 100.2, 150),
                                 bar(10, 3, 101.0, 101.0, 101.0, 101.0, 100)]
    assert mb.take_closed(at(10, 3, 59.95)) == {"XYZ": [bar(10, 0, 100.5, 100.5, 99.8, 99.8, 150),
                                                        bar(10, 1, 100.2, 100.2, 100.2, 100.2, 150)]}
    assert mb.pending("XYZ") == [bar(10, 3, 101.0, 101.0, 101.0, 101.0, 100)]    # still forming
    assert mb.take_closed(at(10, 4, 0)) == {"XYZ": [bar(10, 3, 101.0, 101.0, 101.0, 101.0, 100)]}   # its minute ended
    assert mb.take_closed(at(10, 9, 0)) == {} and mb.pending("XYZ") == []


def test_minute_boundaries_sessions_delays_and_day_volume_resets():
    mb = MinuteBars()
    mb.tick("XYZ", 50.0, 10, False, at(3, 59, 0))           # pre-market not open yet: anchors only
    mb.tick("XYZ", 50.1, 20, False, at(3, 59, 30))
    mb.tick("XYZ", 50.2, 30, False, at(15, 59, 59.9))
    mb.tick("XYZ", 50.3, 40, False, at(16, 0, 0))           # after-hours: still a session minute (04:00-20:00 ET)
    mb.tick("XYZ", 50.4, 50, False, at(20, 0, 1))           # after 20:00 ET: none
    mb.tick("XYZ", 50.5, 60, True, at(16, 5, 0))            # delayed quote: none (its price is minutes old)
    assert [(b.ts.astimezone(NY).strftime("%H:%M:%S"), b.volume) for b in mb.pending("XYZ")] == [("15:59:00", 10),
                                                                                            ("16:00:00", 10)]
    mb = MinuteBars()
    mb.tick("XYZ", 50.0, 9_000, False, at(9, 59, 0, d=16))   # yesterday's day volume
    mb.tick("XYZ", 51.0, 200, False, at(9, 30, 0))           # a lower day volume: a new day, anchored again
    mb.tick("XYZ", 51.5, 260, False, at(9, 30, 10))
    assert mb.pending("XYZ") == [bar(9, 30, 51.5, 51.5, 51.5, 51.5, 60)]
    mb.tick("ABC", 10.0, None, False, at(9, 31, 0))           # without day volumes: a price change is a trade
    mb.tick("ABC", 10.0, None, False, at(9, 31, 5))
    mb.tick("ABC", 10.2, None, False, at(9, 31, 9))
    assert mb.pending("ABC") == [bar(9, 31, 10.2, 10.2, 10.2, 10.2, None)]
    mb.tick("SAT", 1.0, 1, False, at(10, 0, 0, d=21))
    mb.tick("SAT", 1.1, 2, False, at(10, 0, 5, d=21))         # Saturday: none
    assert mb.pending("SAT") == []


async def test_the_hub_builds_bars_from_published_quotes_and_hands_closed_ones_over():
    stored: list = []
    t = [at(11, 0, 10)]

    async def store_bars(b):
        stored.append(b)
    h = LiveHub(lambda: None, lambda: False, None, clock=lambda: t[0], session=lambda _ts: "regular",
                store_bars=store_bars)
    for price, vol, dt in ((200.0, 5000, 10), (200.4, 5100, 30), (200.1, 5100, 35), (199.9, 5300, 55), (200.6, 5400, 61)):
        t[0] = at(11, 0, dt)
        h.publish("questrade", {"XYZ": {"price": price, "volume": vol, "delayed": False}}, {})
    assert h.pending_bars("XYZ") == [bar(11, 0, 200.4, 200.4, 199.9, 199.9, 300), bar(11, 1, 200.6, 200.6, 200.6, 200.6, 100)]
    await h.flush()
    assert stored == [{"XYZ": [bar(11, 0, 200.4, 200.4, 199.9, 199.9, 300)]}]
    assert h.pending_bars("XYZ") == [bar(11, 1, 200.6, 200.6, 200.6, 200.6, 100)]


# ----------------------------------------------------------------------------- serving (the cache)
NOW = ny(2026, 11, 17, 12, 12, 30)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    bars_repo.upsert_bars([Bar("AAPL", date(2026, 11, 13), 1, 1, 1, 1, 1)])
    service._locks.clear()
    yield
    db.close()


@pytest.fixture
async def lagging(tmp_path, tmpdb, monkeypatch):
    """Questrade whose candles end 30 minutes before NOW, and live bars of every minute from 11:30 to now (the
    stored ones up to 12:11, the forming 12:12 one in memory)."""
    fake = FakeQuestrade()
    fake.listed["AAPL"] = 1000
    fake.candle_until = NOW - timedelta(minutes=30)
    src = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=fake.http)
    src.gate.interval = 0.0
    await src.tokens.connect("pasted-token")
    monkeypatch.setattr(chain_source, "REGISTRY", {"questrade": lambda: src})
    minutes = [bar(11, 30 + i, 500 + i, 501 + i, 499 + i, 500.5 + i, 10) for i in range(42)]   # 11:30..12:11
    repo.store_live({"AAPL": minutes}, NOW)
    monkeypatch.setattr(live, "pending_bars", lambda sym: [bar(12, 12, 600, 601, 599, 600.5, 7)] if sym == "AAPL" else [])
    return fake


def candle_starts(fake) -> list[datetime]:
    return [datetime.fromisoformat(r.url.params["startTime"]) for r in fake.requests
            if r.url.path.startswith("/v1/markets/candles/")]


def hm(r) -> str:
    return r["ts"].astimezone(NY).strftime("%H:%M")


async def test_live_bars_follow_the_lagging_candles_and_source_bars_replace_them(lagging):
    fake = lagging
    r = await service.bars("AAPL", "1m", "1d", False, now=NOW)
    ts = [b["ts"] for b in r["bars"]]
    assert ts == [ny(2026, 11, 17, 9, 30) + timedelta(minutes=i) for i in range(len(ts))] and hm(r["bars"][-1]) == "12:12"
    by = {hm(b): b for b in r["bars"]}
    assert by["11:42"]["close"] < 200 and by["11:43"]["close"] == 513.5        # source up to 11:42, live from 11:43
    assert by["11:35"]["close"] < 200                                          # inside the source's bars: never live
    assert {k: v for k, v in by["12:12"].items() if k != "vwap"} == {
        "ts": ny(2026, 11, 17, 12, 12), "open": 600, "high": 601, "low": 599, "close": 600.5, "volume": 7}
    assert r["vwap_source"] == "computed"                       # live-built bars have no VWAP of their own
    assert r["sources"] == ["questrade", "live"] and r["lag_s"] == 29 * 60 + 30   # 11:43:00 -> 12:12:30
    # the span is stored only up to where live bars alone cover it: the next request asks from there again
    assert max(e for _, e in repo.spans("AAPL", "1m")) == ny(2026, 11, 17, 11, 42)
    later = NOW + timedelta(minutes=2)
    fake.candle_until = later - timedelta(minutes=15)                          # the source caught up to 11:59
    r2 = await service.bars("AAPL", "1m", "1d", False, now=later)
    assert candle_starts(fake)[-1] == ny(2026, 11, 17, 11, 42)
    by = {hm(b): b for b in r2["bars"]}
    assert by["11:59"]["close"] < 200 and by["12:00"]["close"] == 530.5 and r2["lag_s"] == 14 * 60 + 30
    stored = {hm(x): x["source"] for x in repo.bars("AAPL", "1m", ny(2026, 11, 17, 11, 30), later)}
    assert stored["11:45"] == "questrade" and stored["12:05"] == "live"       # the source's candle replaced it


async def test_a_live_bar_never_replaces_a_stored_source_candle(lagging):
    await service.bars("AAPL", "1m", "1d", False, now=NOW)
    repo.store_live({"AAPL": [bar(10, 0, 900, 900, 900, 900, 1)]}, NOW)
    (row,) = repo.bars("AAPL", "1m", ny(2026, 11, 17, 10), ny(2026, 11, 17, 10))
    assert row["source"] == "questrade" and row["close"] < 200


@pytest.mark.parametrize("interval,rng,first_live,n_live", [("5m", "1d", "11:45", 6), ("15m", "1d", "11:45", 2),
                                                              ("1h", "5d", "12:00", 1)])
async def test_coarser_intervals_aggregate_the_live_1m_bars(lagging, interval, rng, first_live, n_live):
    r = await service.bars("AAPL", interval, rng, False, now=NOW)
    live_bars = [b for b in r["bars"] if b["close"] > 400]
    assert hm(live_bars[0]) == first_live and len(live_bars) == n_live           # after the source's newest bar only
    assert all(b["close"] < 200 for b in r["bars"][:-n_live])
    step = intraday.INTERVAL_S[interval] // 60
    first = live_bars[0]
    i0 = int(first_live[3:]) + 60 * (int(first_live[:2]) - 11) - 30              # index of its first minute
    minutes = [(500 + i, 501 + i, 499 + i, 500.5 + i, 10) for i in range(i0, min(i0 + step, 42))]
    if i0 + step > 42:
        minutes.append((600, 601, 599, 600.5, 7))                                # the forming 12:12 minute
    assert first["open"] == minutes[0][0] and first["close"] == minutes[-1][3]
    assert first["high"] == max(m[1] for m in minutes) and first["low"] == min(m[2] for m in minutes)
    assert first["volume"] == sum(m[4] for m in minutes)
    assert hm(r["bars"][-1]) == {"5m": "12:10", "15m": "12:00", "1h": "12:00"}[interval]
    assert r["bars"][-1]["close"] == 600.5                                      # the forming minute is the last close
