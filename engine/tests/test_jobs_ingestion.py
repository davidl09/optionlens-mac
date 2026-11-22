"""Regression tests for the ingestion/scheduling rules: metrics finalization, final-data boundary,
EOD bars, close-only index history, split refetch safety and job retry backoff (offline)."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from optionlens import db
from optionlens.analytics.calendar import NY
from optionlens.jobs import _util, chains, ondemand, prices, scheduler
from optionlens.models import Bar
from optionlens.repo import bars as bars_repo
from optionlens.repo import jobs as jobs_repo
from optionlens.repo import metrics as metrics_repo
from optionlens.repo import options as options_repo
from optionlens.repo import quotes as quotes_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.sources import cboe

FIX = Path(__file__).parent / "fixtures"
FRI = date(2026, 11, 20)


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    yield
    db.close()


@pytest.fixture(scope="module")
def payload():
    return json.loads((FIX / "cboe_chain_AAPL.json").read_text())


def snap_at(payload, fetched: datetime, price: float | None = None):
    s = cboe.parse_chain(payload, "AAPL", fetched_at=fetched)
    return s if price is None else dataclasses.replace(s, quote=dataclasses.replace(s.quote, price=price))


# ----------------------------------------------------------------------------- #22 final-data boundary
def test_final_boundary_is_close_plus_feed_delay_everywhere(monkeypatch):
    assert _util.final_since(FRI) == ny(2026, 11, 20, 16, 20)
    assert _util.final_since(date(2026, 11, 27)) == ny(2026, 11, 27, 13, 20)        # early close
    eod = scheduler.JOBS["eod"].schedule              # after the bridge's push window
    assert eod.next_after(ny(2026, 11, 20, 12, 0)) == _util.eod_deadline(FRI) + timedelta(minutes=5)
    assert eod.next_after(ny(2026, 11, 20, 21, 0)) == ny(2026, 11, 23, 20, 5)
    final = scheduler.JOBS["sweep_final"].schedule    # server-side closing snapshot at the boundary
    assert final.next_after(ny(2026, 11, 20, 12, 0)) == _util.final_since(FRI)
    assert final.next_after(ny(2026, 11, 26, 12, 0)) == _util.final_since(date(2026, 11, 27))


def test_is_stale_uses_final_boundary():
    sat = ny(2026, 11, 21, 12, 0)
    assert ondemand.is_stale(ny(2026, 11, 20, 16, 5), sat, 120)        # 16:05 fetch = 15:50 data: not final
    assert not ondemand.is_stale(ny(2026, 11, 20, 16, 20), sat, 120)
    settling = ny(2026, 11, 20, 16, 10)                                # between close and close+20: age-based
    assert not ondemand.is_stale(settling - timedelta(seconds=60), settling, 120)
    assert ondemand.is_stale(settling - timedelta(seconds=600), settling, 120)
    assert ondemand.is_stale(ny(2026, 11, 20, 16, 5), ny(2026, 11, 23, 8, 0), 120)   # Monday pre-open


def test_eod_refuses_before_closing_data_arrived():
    with pytest.raises(ValueError, match="16:20"):
        chains.eod_day(ny(2026, 11, 20, 16, 5))
    assert chains.eod_day(ny(2026, 11, 20, 16, 20)) == FRI


# ----------------------------------------------------------------------------- #2 metrics finalization
def test_writes_metrics_window():
    assert chains.writes_metrics(FRI, ny(2026, 11, 20, 11, 0), False)
    assert chains.writes_metrics(FRI, ny(2026, 11, 20, 16, 10), False)
    assert not chains.writes_metrics(FRI, ny(2026, 11, 20, 16, 20), False)
    assert not chains.writes_metrics(FRI, ny(2026, 11, 21, 12, 0), False)           # Saturday
    assert chains.writes_metrics(FRI, ny(2026, 11, 21, 12, 0), True)                # EOD (catch-up) sweep


def test_late_fetch_keeps_finalized_metrics_row(tmpdb, payload):
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    chains.store_chain(snap_at(payload, ny(2026, 11, 20, 16, 25)), ny(2026, 11, 20, 16, 25), final=True)
    final = metrics_repo.get_metrics("AAPL", FRI)
    assert final["close"] == pytest.approx(348.515)
    for now in (ny(2026, 11, 21, 12, 0), ny(2026, 11, 23, 8, 0)):                  # Saturday, Monday pre-market
        chains.store_chain(snap_at(payload, now, price=400.0), now)
        assert metrics_repo.get_metrics("AAPL", FRI) == final                      # finalized row untouched
        assert quotes_repo.get_quote("AAPL").price == 400.0                       # latest data still refreshed
    assert metrics_repo.dates("AAPL") == [FRI]


def test_late_fetch_creates_an_after_hours_row_and_final_uses_close_as_of(tmpdb, payload):
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    sat = ny(2026, 11, 21, 12, 0)
    chains.store_chain(snap_at(payload, sat), sat)                                 # no row yet: after-hours fill
    fill = metrics_repo.get_metrics("AAPL")
    assert fill["date"] == FRI and fill["after_hours"] is True
    assert options_repo.eod_sources([FRI], ["AAPL"])[FRI] == {}                    # never archived
    chains.store_chain(snap_at(payload, sat), sat, final=True)                     # catch-up EOD on Saturday
    on_sat = metrics_repo.get_metrics("AAPL", FRI)
    assert on_sat["after_hours"] is False                                          # the closing snapshot wins
    at_close = ny(2026, 11, 20, 16, 0)
    chains.store_chain(snap_at(payload, at_close), at_close)
    assert metrics_repo.get_metrics("AAPL", FRI)["iv30"] == pytest.approx(on_sat["iv30"])   # T from the close


# ----------------------------------------------------------------------------- #3 EOD daily bars
def _store_fetch(payload, fetched: datetime) -> None:
    s = snap_at(payload, fetched)
    options_repo.replace_option_latest(s)
    quotes_repo.upsert_quotes([s.quote])


def test_eod_bars_skip_pre_close_fetch(tmpdb, payload):
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])            # on-demand symbol
    _store_fetch(payload, ny(2026, 11, 20, 11, 0))
    assert chains.store_quote_bars(FRI, None) == [] and bars_repo.bars_on(FRI) == {}
    _store_fetch(payload, ny(2026, 11, 20, 16, 10))                               # 15:55 data: still not final
    assert chains.store_quote_bars(FRI, None) == []


def test_eod_bars_replace_on_demand_bar_but_keep_universe_bar(tmpdb, payload):
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    bars_repo.upsert_bars([Bar("AAPL", FRI, 1.0, 1.0, 1.0, 1.0, 1)])             # wrong earlier bar
    _store_fetch(payload, ny(2026, 11, 20, 16, 25))
    assert [b.symbol for b in chains.store_quote_bars(FRI, None)] == ["AAPL"]
    assert bars_repo.bars_on(FRI)["AAPL"].close == pytest.approx(348.515)        # corrected
    symbols_repo.set_universe(["AAPL"])
    bars_repo.upsert_bars([Bar("AAPL", FRI, 350.0, 351.0, 347.0, 349.0, 20_000_000)])   # Nasdaq bar
    chains.store_quote_bars(FRI, None)
    assert bars_repo.bars_on(FRI)["AAPL"].close == 349.0                          # universe: Nasdaq kept


# ----------------------------------------------------------------------------- #6 close-only index history
def test_close_only_bars_keep_eod_ohlc(tmpdb):
    bars_repo.upsert_bars([Bar("SPX", FRI, 6000.0, 6050.0, 5990.0, 6040.0, None)])  # EOD-built OHLC
    prices.store_bars([Bar("SPX", FRI, None, None, None, 6041.5, None),
                       Bar("SPX", date(2026, 11, 19), None, None, None, 6001.0, None)])
    b = bars_repo.bars_on(FRI)["SPX"]
    assert (b.open, b.high, b.low, b.close) == (6000.0, 6050.0, 5990.0, 6041.5)
    assert bars_repo.bars_on(date(2026, 11, 19))["SPX"].close == 6001.0            # missing date inserted


async def test_index_history_is_not_fetched_from_cboe(tmpdb, monkeypatch):
    async def boom(symbol):
        raise AssertionError("CBOE must not be fetched")
    monkeypatch.setattr(cboe, "fetch_history", boom)
    monkeypatch.setattr(prices, "now_ny", lambda: ny(2026, 11, 21, 7, 0))
    assert await prices.fetch_bars("SPX", "index", date(2026, 11, 1), FRI) == []
    assert await prices.refresh_symbol({"symbol": "SPX", "kind": "index"}) == "refreshed"


# ----------------------------------------------------------------------------- #8 split refetch safety
def _history(start: date, end: date, scale: float = 1.0) -> list[Bar]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(Bar("XYZ", d, 100 * scale, 100 * scale, 100 * scale, 100 * scale, 1000))
        d += timedelta(days=1)
    return out


async def test_split_refetch_never_wipes_history(tmpdb, monkeypatch):
    today = date(2026, 11, 21)
    monkeypatch.setattr(prices, "now_ny", lambda: datetime.combine(today, time(7, 0), NY))
    stored_first = date(2020, 1, 2)
    bars_repo.upsert_bars(_history(stored_first, date(2026, 11, 20)))
    full_answer: dict[str, list[Bar]] = {}

    async def fetch(symbol, kind, start, end):
        if start >= today - timedelta(days=prices.REFRESH_DAYS):
            return _history(start, date(2026, 11, 20), 0.5)                        # 2:1 split adjusted
        return full_answer["bars"]
    monkeypatch.setattr(prices, "fetch_bars", fetch)
    row = {"symbol": "XYZ", "kind": "stock"}
    for bad in ([], _history(date(2024, 1, 2), date(2026, 11, 20), 0.5)):         # data:null / truncated
        full_answer["bars"] = bad
        with pytest.raises(ValueError, match="kept"):
            await prices.refresh_symbol(row)
        assert bars_repo.coverage(["XYZ"])["XYZ"]["first"] == stored_first
        assert bars_repo.bars_on(date(2026, 11, 20))["XYZ"].close == 100.0         # untouched
    full_answer["bars"] = _history(stored_first, date(2026, 11, 20), 0.5)
    assert await prices.refresh_symbol(row) == "replaced"
    assert bars_repo.bars_on(date(2026, 11, 20))["XYZ"].close == 50.0


def test_covers_history_for_histories_longer_than_a_backfill():
    today = date(2030, 6, 1)
    full = _history(prices.history_start(today), date(2030, 5, 31))
    assert prices.covers_history(full, date(2016, 1, 4), today)                    # older than 10y: ok
    assert not prices.covers_history(full[300:], date(2016, 1, 4), today)


# ----------------------------------------------------------------------------- #4 retry backoff
def _run(started: datetime, ok: bool | None) -> dict:
    return {"started_at": started, "ok": ok}


def test_owed_run_at_uses_successful_runs_and_backoff():
    job = scheduler.Job("x", None, scheduler.daily(time(6, 0)))
    slot, now = ny(2026, 11, 20, 6, 0), ny(2026, 11, 20, 9, 0)
    assert scheduler.owed_run_at(job, [], now) == now                                       # never ran
    assert scheduler.owed_run_at(job, [_run(slot - timedelta(hours=1), True)], now) == now  # before slot
    assert scheduler.owed_run_at(job, [_run(slot + timedelta(minutes=1), True)], now) is None
    failed = ny(2026, 11, 20, 8, 55)
    assert scheduler.owed_run_at(job, [_run(failed, False)], now) == failed + scheduler.RETRY_BACKOFF
    assert scheduler.owed_run_at(job, [_run(ny(2026, 11, 20, 8, 0), None)], now) == now      # crashed, backoff over
    tries = [_run(slot + timedelta(minutes=m), False) for m in (40, 20, 0)]
    assert scheduler.owed_run_at(job, tries, now) is None                                   # attempts exhausted
    no_catch_up = dataclasses.replace(job, catch_up=False)
    assert scheduler.owed_run_at(no_catch_up, [], now) is None


async def test_failed_job_is_retried_with_backoff_not_hammered(tmpdb, monkeypatch):
    calls: list[int] = []

    async def failing():
        calls.append(1)
        raise RuntimeError("source down")
    job = scheduler.Job("universe", failing, scheduler.daily(time(0, 0)))
    monkeypatch.setattr(scheduler, "JOBS", {"universe": job})
    monkeypatch.setattr(scheduler, "RETRY_BACKOFF", timedelta(milliseconds=50))
    await scheduler.start()
    try:
        await asyncio.sleep(0.6)
    finally:
        await scheduler.stop()
    assert len(calls) == scheduler.MAX_ATTEMPTS                                    # retried, then gave up
    runs = jobs_repo.recent_runs(10, "universe")
    assert [r["ok"] for r in runs] == [False] * scheduler.MAX_ATTEMPTS
    assert not await scheduler.missed(job, datetime.now(timezone.utc))


# ----------------------------------------------------------------------------- #25 sweep interval
def test_intraday_rejects_non_positive_interval():
    for bad in (0, -5):
        with pytest.raises(ValueError):
            scheduler.intraday(bad)
