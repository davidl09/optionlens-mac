"""Scheduler timing, job recording, and the sweep/eod/backfill pipelines with mocked fetches (offline)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from optionlens import db
from optionlens.analytics.calendar import NY
from optionlens.jobs import calendars, chains, hooks, ondemand, prices, scheduler
from optionlens.models import Bar, EarningsEvent
from optionlens.net import FetchError
from optionlens.repo import bars as bars_repo
from optionlens.repo import jobs as jobs_repo
from optionlens.repo import metrics as metrics_repo
from optionlens.repo import options as options_repo
from optionlens.repo import status as status_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.sources import cboe, chain_source, nasdaq

FIX = Path(__file__).parent / "fixtures"


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    yield
    db.close()


# ----------------------------------------------------------------------------- schedules
def test_intraday_sweep_schedule():
    s = scheduler.intraday(30)
    assert s.next_after(ny(2026, 11, 19, 9, 0)) == ny(2026, 11, 19, 9, 45)
    assert s.next_after(ny(2026, 11, 19, 9, 45)) == ny(2026, 11, 19, 10, 15)
    assert s.next_after(ny(2026, 11, 19, 15, 50)) == ny(2026, 11, 20, 9, 45)       # Thu -> Fri
    assert s.next_after(ny(2026, 11, 20, 16, 0)) == ny(2026, 11, 23, 9, 45)        # skip weekend
    # trading days of any exchange followed (NYSE or TSX): US Thanksgiving is a TSX day, Good Friday closes both
    assert s.next_after(ny(2026, 11, 25, 16, 0)) == ny(2026, 11, 26, 9, 45)
    assert s.next_after(ny(2026, 4, 2, 16, 0)) == ny(2026, 4, 6, 9, 45)
    early = s.times_on(date(2026, 11, 27))                    # NYSE closes 13:00, TSX 16:00: until the later close
    assert early[0] == time(9, 45) and early[-1] == time(15, 45)
    xmas_eve = s.times_on(date(2026, 12, 24))                 # both close at 13:00
    assert xmas_eve[-1] == time(12, 45)
    assert scheduler.intraday(30).times_on(date(2026, 11, 19))[-1] == time(15, 45)
    assert scheduler.intraday(15).times_on(date(2026, 11, 19))[-1] == time(16, 0)  # inclusive close
    assert s.prev_before(ny(2026, 11, 23, 8, 0)) == ny(2026, 11, 20, 15, 45)
    assert s.next_after(datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)) == ny(2026, 11, 19, 10, 15)  # 10:00 EST


def test_eod_and_daily_schedules():
    eod = scheduler.after_close(20)
    assert eod.next_after(ny(2026, 11, 19, 12, 0)) == ny(2026, 11, 19, 16, 20)
    assert eod.next_after(ny(2026, 11, 25, 17, 0)) == ny(2026, 11, 26, 16, 20)     # US Thanksgiving: TSX closes
    assert eod.times_on(date(2026, 11, 27)) == [time(13, 20), time(16, 20)]        # NYSE early close, TSX full day
    assert eod.next_after(ny(2026, 12, 24, 9, 0)) == ny(2026, 12, 24, 13, 20)      # Christmas Eve early close
    assert eod.next_after(ny(2026, 12, 24, 14, 0)) == ny(2026, 12, 28, 16, 20)     # Christmas + weekend
    uni = scheduler.daily(time(6, 0), {scheduler.MON})
    assert uni.next_after(ny(2026, 11, 19, 12, 0)) == ny(2026, 11, 23, 6, 0)
    assert uni.prev_before(ny(2026, 11, 19, 12, 0)) == ny(2026, 11, 16, 6, 0)
    vix = scheduler.daily(time(17, 0))
    assert vix.next_after(ny(2026, 11, 21, 17, 0)) == ny(2026, 11, 22, 17, 0)      # daily incl. weekend
    td = scheduler.daily(time(17, 0), trading_only=True)
    assert td.next_after(ny(2026, 11, 21, 12, 0)) == ny(2026, 11, 23, 17, 0)
    # DST: 2026-11-01 ends DST; a 03:00 job still fires at 03:00 local
    assert scheduler.daily(time(3, 0)).next_after(ny(2026, 11, 1, 0, 30)).hour == 3


def test_all_spec_jobs_defined():
    spec = {"universe", "prices_backfill", "prices_refresh", "earnings", "earnings_backfill", "earnings_ca",
            "earnings_backfill_ca",
            "dividends", "dividends_history", "vix", "rates", "rates_cad", "universe_ca", "sweep", "sweep_final",
            "sweep_fill", "eod", "prune", "splits", "extended_quotes"}
    # services.alerts.install() may add its own digest job when it has been imported by other tests
    assert set(scheduler.JOBS) - {"digest_morning"} == spec
    # no CBOE collection; eod after the bridge's pushes
    assert scheduler.next_run_at("eod", ny(2026, 11, 19, 12, 0)) == ny(2026, 11, 19, 20, 5)
    assert scheduler.next_run_at("vix", ny(2026, 11, 19, 12, 0)) is None and not scheduler.JOBS["vix"].catch_up


def test_server_source_schedules_sweeps(monkeypatch):
    jobs = scheduler.build_jobs()                                  # default: Questrade configured
    assert jobs["sweep"].catch_up and jobs["sweep"].schedule.next_after(ny(2026, 11, 19, 9, 0)) == ny(2026, 11, 19, 9, 45)
    assert jobs["sweep_final"].schedule.next_after(ny(2026, 11, 19, 12, 0)) == ny(2026, 11, 19, 16, 20)
    assert jobs["sweep_fill"].schedule.next_after(ny(2026, 11, 21, 12, 0)) == ny(2026, 11, 21, 12, 35)
    assert jobs["eod"].schedule.next_after(ny(2026, 11, 19, 12, 0)) == ny(2026, 11, 19, 20, 5)
    ext = jobs["extended_quotes"].schedule                         # pre-market and after-hours, every 5 min
    assert ext.next_after(ny(2026, 11, 19, 3, 0)) == ny(2026, 11, 19, 4, 0)
    assert ext.next_after(ny(2026, 11, 19, 9, 25)) == ny(2026, 11, 19, 16, 0)
    assert ext.next_after(ny(2026, 11, 19, 19, 55)) == ny(2026, 11, 20, 4, 0)
    assert ext.next_after(ny(2026, 11, 27, 13, 2)) == ny(2026, 11, 27, 13, 5)   # early close (day after Thanksgiving)
    assert not jobs["extended_quotes"].catch_up
    from optionlens import config
    monkeypatch.setattr(chain_source, "get_settings", lambda: type("S", (), {"chain_sources": ("ibkr-bridge",)})())
    jobs = scheduler.build_jobs()                                  # bridge only: sweeps are manual
    for name in ("sweep", "sweep_final", "sweep_fill", "extended_quotes"):
        assert jobs[name].schedule.next_after(ny(2026, 11, 19, 12, 0)) is None and not jobs[name].catch_up
    assert config  # noqa


def test_chain_source_registry(monkeypatch):
    assert chain_source.server_source_names() == ["questrade"]     # default: ibkr-bridge, questrade
    assert chain_source.tag("ibkr-bridge") == "ibkr" and chain_source.tag("questrade") == "questrade"
    monkeypatch.setattr(chain_source, "get_settings", lambda: type("S", (), {"chain_sources": ("acme", "ibkr-bridge")})())
    with pytest.raises(ValueError, match="acme"):
        chain_source.server_sources()
    monkeypatch.setitem(chain_source.REGISTRY, "acme", lambda: "SRC")
    assert chain_source.server_sources() == ["SRC"]


# ----------------------------------------------------------------------------- run_job
async def test_run_job_records_success_failure_and_no_overlap(tmpdb, monkeypatch):
    calls = []

    async def ok_job():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "done"

    async def bad_job():
        raise RuntimeError("boom")

    monkeypatch.setitem(scheduler.JOBS, "vix", scheduler.Job("vix", ok_job, scheduler.daily(time(17, 0))))
    monkeypatch.setitem(scheduler.JOBS, "rates", scheduler.Job("rates", bad_job, scheduler.daily(time(17, 5))))
    (r1, r2) = await asyncio.gather(scheduler.run_job("vix"), scheduler.run_job("vix"))
    assert sorted([r1, r2]) == [(False, "already running"), (True, "done")] and len(calls) == 1
    assert await scheduler.run_job("rates") == (False, "RuntimeError: boom")
    runs = {r["job"]: r for r in jobs_repo.recent_runs()}
    assert runs["vix"]["ok"] is True and runs["vix"]["detail"] == "done"
    assert runs["rates"]["ok"] is False and runs["rates"]["finished_at"] is not None
    assert not await scheduler.missed(scheduler.JOBS["vix"], datetime.now(NY))
    assert await scheduler.missed(scheduler.JOBS["prune"], datetime.now(NY))


# ----------------------------------------------------------------------------- sweep / eod pipeline
FETCHED = ny(2026, 11, 19, 16, 25)


class FakeVendor:
    """A ChainSource serving the recorded AAPL chain (CBOE fixture parsed offline)."""
    name = "vendor"

    def __init__(self):
        self.payload = json.loads((FIX / "cboe_chain_AAPL.json").read_text())
        self.calls: list[str] = []
        self.delay = 0.0

    def connected(self):
        return True

    def unavailable(self):
        return None

    async def keepalive(self):
        return None

    def status(self):
        return {}

    async def fetch_chain(self, symbol, final=False):
        self.calls.append(symbol)
        await asyncio.sleep(self.delay)
        if symbol == "AAPL":
            return cboe.parse_chain(self.payload, "AAPL", fetched_at=FETCHED)
        if symbol == "EMPTY":
            p = {**self.payload, "data": {**self.payload["data"], "options": []}}
            return cboe.parse_chain(p, "EMPTY", fetched_at=FETCHED)
        if symbol in ("SPX", "NDX", "RUT", "XSP"):
            raise chain_source.NotListed(symbol)
        raise FetchError("u", 403 if symbol == "NOPE" else 503)


@pytest.fixture
def mocked_vendor(monkeypatch):
    v = FakeVendor()
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: v)
    monkeypatch.setattr(chains, "now_ny", lambda: FETCHED)
    return v


def _seed_symbols_and_bars():
    symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"} for s in ("AAPL", "NOPE", "EMPTY", "FLAKY")])
    symbols_repo.set_universe(["AAPL", "NOPE", "EMPTY", "FLAKY"])
    hist = nasdaq.parse_history(json.loads((FIX / "nasdaq_history_AAPL.json").read_text()), "AAPL")
    bars_repo.upsert_bars(hist)          # through 2026-11-18


async def test_sweep_needs_a_chain_source(tmpdb, monkeypatch):
    assert await chains.sweep(["AAPL"]) == "questrade: not connected"              # configured, not connected
    monkeypatch.setattr(chain_source, "get_settings", lambda: type("S", (), {"chain_sources": ("ibkr-bridge",)})())
    with pytest.raises(RuntimeError, match="no server-side chain source"):
        await chains.sweep(["AAPL"])


async def test_sweep_pipeline(tmpdb, mocked_vendor, monkeypatch):
    _seed_symbols_and_bars()
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 11, 19, 15, 50))   # intraday sweep writes metrics
    seen = []
    hooks.on_sweep_complete.append(lambda day, syms: seen.append((day, syms)))
    try:
        detail = await chains.sweep()
    finally:
        hooks.on_sweep_complete.clear()
    assert detail.startswith("vendor: 1 chains, 1 not optionable, 4 not listed, 2 failed")   # indices: not listed
    assert "(failures: 1 × 503 FetchError; 1 × 403 FetchError)" in detail                # grouped reasons
    assert seen == [(date(2026, 11, 19), ["AAPL"])]
    assert status_repo.row_counts()["option_latest"] == 3386
    assert symbols_repo.get_symbol("AAPL")["optionable"] is True
    assert symbols_repo.get_symbol("NOPE")["optionable"] is None     # an error never marks unoptionable
    assert symbols_repo.get_symbol("EMPTY")["optionable"] is False
    assert symbols_repo.get_symbol("FLAKY")["optionable"] is None   # transient failure: unchanged
    assert symbols_repo.universe_symbols() == ["AAPL", "FLAKY", "NOPE"]
    assert options_repo.latest_source("AAPL") == "vendor" and metrics_repo.source_of(date(2026, 11, 19), "AAPL") == "vendor"
    m = metrics_repo.get_metrics("AAPL", date(2026, 11, 19))
    assert 0.15 < m["iv30"] < 0.6 and m["iv30_cboe"] == pytest.approx(0.25007)
    assert m["close"] == pytest.approx(348.515) and m["volume"] == 18174222
    assert m["call_volume"] > 0 and m["put_oi"] > 0 and m["hv20"] is not None and m["hv252"] is not None
    # HV uses stored closes + today's live price as the last close
    closes = np.append(bars_repo.get_bars("AAPL")["close"][-21:], 348.515)
    from optionlens.analytics import vol
    assert m["hv20"] == pytest.approx(vol.hv_snapshot(closes)["hv20"])


async def test_eod_writes_archive_and_bars(tmpdb, mocked_vendor, monkeypatch):
    _seed_symbols_and_bars()
    got = []
    hooks.on_eod.append(lambda day, syms: got.append((day, syms)))

    async def async_hook(day, syms):
        raise RuntimeError("hooks must not break eod")
    hooks.on_eod.append(async_hook)
    try:
        assert (await chains.sweep_final()).startswith("vendor: 1 chains")          # closing snapshot, archived
        assert options_repo.eod_sources([date(2026, 11, 19)]) == {date(2026, 11, 19): {"AAPL": "vendor"}}
        detail = await chains.eod()
    finally:
        hooks.on_eod.clear()
    day = date(2026, 11, 19)
    assert detail.startswith("2026-11-19:")
    assert got == [(day, ["AAPL"])]   # bars from final chain fetches only (EMPTY: quote without a chain)
    n = status_repo.row_counts()["option_eod"]
    assert 0 < n <= 3386 and options_repo.eod_dates() == [day]
    b = bars_repo.bars_on(day)["AAPL"]
    assert (b.open, b.high, b.low, b.close, b.volume) == (352.97, 355.28, 346.48, 348.515, 18174222)


def test_eod_refuses_while_market_open():
    with pytest.raises(ValueError):
        chains.eod_day(ny(2026, 11, 19, 11, 0))
    assert chains.eod_day(ny(2026, 11, 21, 10, 0)) == date(2026, 11, 20)
    assert chains.eod_day(ny(2026, 11, 23, 8, 0)) == date(2026, 11, 20)


# ----------------------------------------------------------------------------- on-demand
async def test_refresh_round_dedupes_and_creates_symbol(tmpdb, mocked_vendor, monkeypatch):
    """Five page loads at once: one visit (the symbol is created), one round, one fetch from the server source."""
    from optionlens.services import refresh
    mocked_vendor.delay = 0.02
    ondemand._viewed.clear()

    async def no_history(row, start=None):
        return 0

    async def info(symbol, asset="stocks"):
        return {"name": "Apple Inc."} if asset == "stocks" else None

    monkeypatch.setattr(prices, "backfill_symbol", no_history)
    monkeypatch.setattr(nasdaq, "fetch_info", info)
    res = await asyncio.gather(*(refresh.start("aapl") for _ in range(5)))
    assert all(r["pending"] for r in res)
    await asyncio.gather(*refresh._tasks)
    assert mocked_vendor.calls == ["AAPL"]
    st = refresh.status("AAPL")
    assert not st["pending"] and st["sources"][0]["name"] == "vendor" and st["sources"][0]["state"] == "ok"
    assert st["served"]["source"] == "vendor"
    from optionlens.repo import views as views_repo
    assert views_repo.viewed_since(datetime(2000, 1, 1, tzinfo=timezone.utc)) == ["AAPL"]
    row = symbols_repo.get_symbol("AAPL")
    assert row["kind"] == "stock" and row["in_universe"] is False and row["optionable"] is True


async def test_fetch_cached_caches_no_data_answers_for_negative_ttl(tmpdb, monkeypatch):
    symbols_repo.upsert_symbols([{"symbol": "SPY", "name": "SPDR S&P 500", "kind": "etf"}])
    calls = []

    async def no_profile(symbol):
        calls.append(symbol)
        return None

    monkeypatch.setattr(nasdaq, "fetch_profile", no_profile)
    assert await ondemand.fetch_cached("profile", "SPY") is None
    assert await ondemand.fetch_cached("profile", "spy") is None
    assert calls == ["SPY"]                                       # the None answer was cached
    payload, fetched = ondemand.peek_cached("profile", "SPY")
    assert payload is None and fetched.tzinfo
    real_now = datetime.now(timezone.utc)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now + timedelta(seconds=ondemand.NEGATIVE_TTL_S + 60)

    monkeypatch.setattr(ondemand, "datetime", Later)
    assert await ondemand.fetch_cached("profile", "SPY") is None
    assert calls == ["SPY", "SPY"]                                # re-asked after the negative TTL


async def test_fetch_cached_keeps_older_real_payload_over_no_data(tmpdb, monkeypatch):
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "name": "Apple", "kind": "stock"}])
    answers = [{"CompanyName": "Apple Inc."}, None]

    async def profile(symbol):
        return answers.pop(0)

    monkeypatch.setattr(nasdaq, "fetch_profile", profile)
    monkeypatch.setitem(ondemand.CACHE_TTL_S, "profile", 0)      # always stale -> always refetch
    assert await ondemand.fetch_cached("profile", "AAPL") == {"CompanyName": "Apple Inc."}
    assert await ondemand.fetch_cached("profile", "AAPL") == {"CompanyName": "Apple Inc."}
    assert ondemand.peek_cached("profile", "AAPL")[0] == {"CompanyName": "Apple Inc."}


# ----------------------------------------------------------------------------- prices
def test_plan_backfill_and_split_detection():
    rows = [{"symbol": "NEW", "kind": "stock"}, {"symbol": "OLD", "kind": "stock"}, {"symbol": "OK", "kind": "etf"}]
    cov = {"OLD": {"count": 10, "first": date(2016, 1, 1), "last": date(2026, 11, 10)},
           "OK": {"count": 10, "first": date(2016, 1, 1), "last": date(2026, 11, 18)}}
    plan = prices.plan_backfill(rows, cov, date(2026, 11, 19), date(2026, 11, 18))
    assert [(r["symbol"], s) for r, s in plan] == [("NEW", date(2016, 11, 18)), ("OLD", date(2026, 11, 3))]
    stored = {"date": np.array(["2026-11-17", "2026-11-18"], dtype="datetime64[D]"), "close": np.array([100.0, 102.0])}
    same = [Bar("X", date(2026, 11, 17), None, None, None, 100.0, None), Bar("X", date(2026, 11, 18), None, None, None, 102.1, None)]
    split = [Bar("X", date(2026, 11, 17), None, None, None, 50.0, None), Bar("X", date(2026, 11, 18), None, None, None, 51.0, None)]
    assert not prices.adjusted_since_stored(stored, same)
    assert prices.adjusted_since_stored(stored, split)


# ----------------------------------------------------------------------------- earnings backfill
async def test_earnings_backfill_is_resumable(tmpdb, monkeypatch):
    today = date(2026, 11, 19)
    monkeypatch.setattr(calendars, "now_ny", lambda: ny(2026, 11, 19, 12, 0))
    monkeypatch.setattr(calendars, "BACKFILL_YEARS", 0.2)   # ~73 days window -> ~45 weekdays
    monkeypatch.setattr(calendars, "PROGRESS_EVERY", 5)
    fetched: list[date] = []
    fail_at = {"n": 12}

    async def fake(d):
        if len(fetched) == fail_at["n"]:
            raise FetchError("u", 500)
        fetched.append(d)
        return [EarningsEvent("AAA", d, "bmo", None, None, None, None, None)]

    monkeypatch.setattr(nasdaq, "fetch_earnings", fake)
    assert calendars.earnings_backfill_progress(today) == 0.0
    ok, _ = await scheduler.run_job("earnings_backfill")
    assert not ok and len(fetched) == 12
    p1 = calendars.earnings_backfill_progress(today)
    assert p1 == pytest.approx(12 / 45, abs=0.05)  # failed run still records its cursor
    fail_at["n"] = -1
    ok, detail = await scheduler.run_job("earnings_backfill")
    assert ok and calendars.earnings_backfill_progress(today) == 1.0
    start, end = calendars._backfill_window(today)
    weekdays = calendars.days_between(start, end, weekdays_only=True)
    assert sorted(fetched) == weekdays          # every weekday fetched exactly once across both runs
    assert fetched[0] == weekdays[-1]           # newest first
    ok, detail = await scheduler.run_job("earnings_backfill")   # complete: no-op
    assert ok and json.loads(detail)["events"] == 0


async def test_scheduler_start_bootstraps_and_stops(tmpdb, monkeypatch):
    ran: list[str] = []

    def fake(name):
        async def fn():
            ran.append(name)
            return name
        return fn

    jobs = {n: scheduler.Job(n, fake(n), j.schedule, j.catch_up) for n, j in scheduler.JOBS.items() if j.catch_up}
    monkeypatch.setattr(scheduler, "JOBS", jobs)   # (non-catch-up jobs such as digests never run at start)
    await scheduler.start()
    await scheduler.start()                      # idempotent
    for _ in range(100):
        if len(set(ran)) == len(jobs):
            break
        await asyncio.sleep(0.02)
    await scheduler.stop()
    assert ran[0] == "universe" and set(ran) == set(jobs)   # every job missed on a fresh db -> caught up once
    assert len(ran) == len(jobs)
    assert {r["job"] for r in jobs_repo.recent_runs(100)} == set(jobs)


async def test_fetch_calendar_caches_6h_and_serves_stale_on_failure(tmpdb, monkeypatch):
    answers = [[{"symbol": "AAA"}], FetchError("u", 503)]
    calls = []

    async def ipos(month):
        calls.append(month)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(nasdaq, "fetch_ipos", ipos)
    assert await ondemand.fetch_calendar("ipos", month="2026-11") == [{"symbol": "AAA"}]
    assert await ondemand.fetch_calendar("ipos", month="2026-11") == [{"symbol": "AAA"}]
    assert calls == ["2026-11"]                                   # second call served from http_cache
    monkeypatch.setattr(ondemand, "CALENDAR_TTL_S", 0)
    assert await ondemand.fetch_calendar("ipos", month="2026-11") == [{"symbol": "AAA"}]   # refetch failed -> stale
    assert calls == ["2026-11", "2026-11"]
    with pytest.raises(KeyError):
        await ondemand.fetch_calendar("nope")
