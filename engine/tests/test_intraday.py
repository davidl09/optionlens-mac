"""Intraday bars: windows and sessions (pure), Questrade candle paging, the cache (only missing spans fetched,
retention) and the IBKR fallback (spans pushed by the bridge), offline."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from optionlens import db
from optionlens.analytics import intraday
from optionlens.analytics.calendar import NY
from optionlens.models import Bar, IntradayBar
from optionlens.repo import bars as bars_repo
from optionlens.repo import intraday as repo
from optionlens.repo import symbols as symbols_repo
from optionlens.services import intraday as service
from optionlens.sources import chain_source
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade

TUE = datetime(2026, 11, 17, 14, 0, tzinfo=NY)          # a Tuesday, mid-session
SAT = datetime(2026, 11, 21, 12, 0, tzinfo=NY)


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


# ----------------------------------------------------------------------------- pure
def test_offered_combinations():
    intraday.check("1m", "1d")
    intraday.check("1h", "3m")
    for interval, rng in (("1m", "5d"), ("5m", "3m"), ("1d", "1d"), ("1m", "1y")):
        with pytest.raises(ValueError):
            intraday.check(interval, rng)


def test_window_starts_at_the_first_session_premarket():
    assert intraday.window("1d", TUE, False)[0] == ny(2026, 11, 17, 4, 0)
    assert intraday.window("5d", TUE, False)[0] == ny(2026, 11, 11, 4, 0)       # Wed..Tue (5 sessions)
    assert intraday.window("1d", SAT, False)[0] == ny(2026, 11, 20, 4, 0)       # weekend: Friday's session
    early = ny(2026, 11, 17, 8, 0)                                               # before the open
    assert intraday.window("1d", early, False)[0] == ny(2026, 11, 16, 4, 0)     # regular view: Monday
    assert intraday.window("1d", early, True)[0] == ny(2026, 11, 17, 4, 0)      # extended: today's pre-market
    assert intraday.window("1m", TUE, False)[0] == ny(2026, 10, 17, 4, 0)
    assert intraday.window("3m", ny(2026, 5, 31, 12), False)[0] == ny(2026, 2, 28, 4, 0)   # month-end clamps


def test_regular_hours_and_missing_spans():
    assert intraday.is_regular(ny(2026, 11, 17, 9, 30)) and not intraday.is_regular(ny(2026, 11, 17, 9, 29))
    assert not intraday.is_regular(ny(2026, 11, 17, 16, 0)) and not intraday.is_regular(ny(2026, 11, 21, 12))
    assert not intraday.is_regular(ny(2026, 11, 27, 13, 0))                     # early close (day after Thanksgiving)
    a, b, c, d = (ny(2026, 11, 17, h) for h in (4, 8, 12, 20))
    assert intraday.missing([], (a, d)) == [(a, d)]
    assert intraday.missing([(b, c)], (a, d)) == [(a, b), (c, d)]
    assert intraday.missing([(a, c), (b, d)], (a, d)) == []


def test_request_windows_hold_at_most_the_limit_and_skip_nights():
    span = (ny(2026, 11, 16, 0), ny(2026, 11, 21, 0))                           # Mon..Fri
    for interval, cap in (("1m", 2000), ("5m", 2000), ("1m", 500)):
        wins = intraday.request_windows(span, interval, cap)
        step = intraday.INTERVAL_S[interval]
        assert all((b - a).total_seconds() / step <= cap for a, b in intraday.session_windows((wins[0][0], wins[0][1]))[:1])
        per_window = [sum(-(-(y - x).total_seconds() // step) for x, y in intraday.session_windows(w)) for w in wins]
        assert max(per_window) <= cap and sum(per_window) == 5 * 16 * 3600 / step
    assert len(intraday.request_windows(span, "1m", 2000)) == 3                 # 960 bars a day: 2 + 2 + 1 days
    assert len(intraday.request_windows(span, "5m", 2000)) == 1
    assert intraday.request_windows((ny(2026, 11, 21, 0), ny(2026, 11, 23, 0)), "1m", 2000) == []   # weekend


# ----------------------------------------------------------------------------- Questrade candles
@pytest.fixture
async def questrade(tmp_path):
    fake = FakeQuestrade()
    fake.listed["AAPL"] = 1000
    src = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=fake.http)
    src.gate.interval = 0.0
    await src.tokens.connect("pasted-token")
    return fake, src


def candle_calls(fake: FakeQuestrade) -> list:
    return [r for r in fake.requests if r.url.path.startswith("/v1/markets/candles/")]


async def test_candles_are_paged_by_time_windows(questrade):
    fake, src = questrade
    start, end = ny(2026, 11, 16, 0), ny(2026, 11, 20, 23)
    bars = await src.fetch_candles("AAPL", "1m", start, end)
    calls = candle_calls(fake)
    assert len(calls) == 3 and len(bars) == 5 * 16 * 60
    p = calls[0].url.params
    assert p["interval"] == "OneMinute" and p["startTime"] == "2026-11-16T04:00:00-05:00"
    assert p["endTime"] == "2026-11-17T20:00:00-05:00"                          # two days: 1,920 candles
    assert bars == sorted(bars, key=lambda b: b.ts) and len({b.ts for b in bars}) == len(bars)
    assert all(b.ts.tzinfo is not None for b in bars)


async def test_a_full_response_is_continued_after_its_last_candle(questrade):
    fake, src = questrade
    fake.candle_hours = (0, 24)           # the source has night candles too: a two-day window overflows 2,000
    bars = await src.fetch_candles("AAPL", "1m", ny(2026, 11, 16, 4), ny(2026, 11, 17, 20))
    calls = candle_calls(fake)
    assert len(calls) == 2 and len(bars) == 40 * 60 + 1
    assert calls[1].url.params["startTime"] == "2026-11-17T13:20:00-05:00"      # after the 2,000th candle


# ----------------------------------------------------------------------------- the cache
@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    bars_repo.upsert_bars([Bar("AAPL", date(2026, 11, 13), 1, 1, 1, 1, 1)])
    service._locks.clear()
    yield
    db.close()


async def test_only_missing_spans_are_fetched(tmpdb, questrade, monkeypatch):
    fake, src = questrade
    monkeypatch.setattr(chain_source, "REGISTRY", {"questrade": lambda: src})
    r = await service.bars("AAPL", "5m", "5d", False, now=TUE)
    assert r["sources"] == ["questrade"] and r["missing"] == [] and r["problem"] is None
    assert r["has_extended"] is True and all(intraday.is_regular(b["ts"]) for b in r["bars"])
    assert r["bars"][0]["ts"] == ny(2026, 11, 11, 9, 30) and r["bars"][-1]["ts"] <= TUE
    n = len(candle_calls(fake))
    again = await service.bars("AAPL", "5m", "5d", False, now=TUE + timedelta(minutes=5))
    later = candle_calls(fake)[n:]
    assert len(later) == 1                                                       # only the unsettled tail
    assert datetime.fromisoformat(later[0].url.params["startTime"]) >= TUE - intraday.RECENT
    assert len(again["bars"]) == len(r["bars"]) + 1
    ext = await service.bars("AAPL", "5m", "5d", True, now=TUE + timedelta(minutes=5))
    assert len(candle_calls(fake)) == n + 2 and len(ext["bars"]) > len(again["bars"])
    assert ext["bars"][0]["ts"] == ny(2026, 11, 11, 4, 0)


async def test_retention_drops_old_bars(tmpdb):
    old = IntradayBar(TUE - timedelta(days=11), 1, 1, 1, 1, 1)
    new = IntradayBar(TUE - timedelta(hours=1), 1, 1, 1, 1, 1)
    repo.store("AAPL", "1m", [old, new], [(old.ts, new.ts)], "ibkr", None, TUE, TUE - intraday.RETENTION["1m"])
    assert [b["ts"] for b in repo.bars("AAPL", "1m", TUE - timedelta(days=30), TUE)] == [new.ts]
    assert repo.spans("AAPL", "1m") == [(old.ts, new.ts)]                        # still reaches into the kept part


async def test_without_questrade_the_spans_are_missing_until_the_bridge_pushes_them(tmpdb):
    r = await service.bars("AAPL", "15m", "1d", False, now=TUE)
    assert r["bars"] == [] and r["sources"] == [] and r["problem"] is None      # (tests: an offline stand-in source)
    assert [(m["start"], m["end"]) for m in r["missing"]] == [(ny(2026, 11, 17, 4), TUE)]
    pushed = [{"ts": ny(2026, 11, 17, 9, 30) + timedelta(minutes=15 * i), "open": 10, "high": 11, "low": 9, "close": 10.5,
               "volume": 100} for i in range(18)]
    out = service.store_pushed({"source": "ibkr", "symbol": "AAPL", "interval": "15m", "delayed": False,
                                "spans": [{"start": m["start"], "end": m["end"]} for m in r["missing"]], "bars": pushed}, now=TUE)
    assert out == {"stored": 18, "symbol": "AAPL"}
    r = await service.bars("AAPL", "15m", "1d", False, now=TUE)
    assert len(r["bars"]) == 18 and r["sources"] == ["ibkr"] and r["delayed"] is False
    assert [(m["start"], m["end"]) for m in r["missing"]] == [(TUE - intraday.RECENT, TUE)]   # the unsettled tail


async def test_an_unusable_questrade_is_named(tmpdb, tmp_path, monkeypatch):
    src = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=FakeQuestrade().http)   # no token pasted
    monkeypatch.setattr(chain_source, "REGISTRY", {"questrade": lambda: src})
    r = await service.bars("AAPL", "1h", "3m", False, now=SAT)
    assert r["problem"].startswith("questrade: ") and len(r["missing"]) == 1


# ----------------------------------------------------------------------------- session VWAP
def test_day_vwap_uses_candle_vwaps_else_typical_prices_and_resets_each_session():
    from optionlens.analytics.calendar import NY as _NY

    def bar(d, hh, mm, vol, vwap=None, h=11.0, lo=9.0, c=10.0):
        return {"ts": datetime(2026, 11, d, hh, mm, tzinfo=_NY), "high": h, "low": lo, "close": c, "volume": vol,
                "vwap": vwap}
    own = [bar(17, 9, 30, 100, 10.0), bar(17, 9, 35, 300, 12.0), bar(17, 16, 5, 50, 99.0)]    # 16:05: after hours
    vals, how = intraday.day_vwap(own)
    assert vals == [10.0, pytest.approx((1000 + 3600) / 400), None] and how == "candles"
    mixed = [bar(17, 9, 30, 100, 10.0), bar(17, 9, 35, 100, None, h=13.0, lo=11.0, c=12.0), bar(18, 9, 30, 10, 20.0)]
    vals, how = intraday.day_vwap(mixed)
    assert vals == [10.0, pytest.approx(11.0), 20.0] and how == "computed"                    # a new session restarts
    assert intraday.day_vwap([bar(17, 9, 30, None)]) == ([None], None)                         # no volume: unknown


def test_day_vwap_with_extended_hours_runs_from_the_pre_market_through_after_hours():
    from optionlens.analytics.calendar import NY as _NY

    def bar(d, hh, mm, vol, vwap):
        return {"ts": datetime(2026, 11, d, hh, mm, tzinfo=_NY), "high": vwap, "low": vwap, "close": vwap,
                "volume": vol, "vwap": vwap}
    bars = [bar(17, 7, 0, 100, 8.0), bar(17, 9, 30, 100, 10.0), bar(17, 16, 5, 200, 13.0), bar(18, 4, 0, 10, 20.0)]
    assert intraday.day_vwap(bars)[0] == [None, 10.0, None, None]                                   # regular hours only
    vals, how = intraday.day_vwap(bars, extended=True)
    assert vals == [8.0, 9.0, pytest.approx((800 + 1000 + 2600) / 400), 20.0] and how == "candles"  # next day restarts
