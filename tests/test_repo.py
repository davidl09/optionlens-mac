"""Repo round-trips against a temporary DuckDB file."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from optionlens import db
from optionlens.models import Bar, Dividend, EarningsEvent
from optionlens.repo import (bars, cache, dividends, earnings, jobs, market, metrics, options, quotes,
                             report_snapshots, status, symbols, user)
from optionlens.sources import cboe

FIX = Path(__file__).parent / "fixtures"
UTC = timezone.utc


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    yield
    db.close()


@pytest.fixture(scope="module")
def aapl():
    return cboe.parse_chain(json.loads((FIX / "cboe_chain_AAPL.json").read_text()), "AAPL",
                            fetched_at=datetime(2026, 11, 19, 21, 30, tzinfo=UTC))


def test_schema_is_idempotent(tmp_path):
    db.close()
    db.connect(tmp_path / "x.duckdb")
    db.close()
    db.connect(tmp_path / "x.duckdb")
    assert status.row_counts()["symbols"] == 0
    db.close()


# ----------------------------------------------------------------------------- symbols
def test_symbols_upsert_universe_search(tmpdb):
    symbols.upsert_symbols([
        {"symbol": "AAPL", "name": "Apple Inc.", "kind": "stock", "sector": "Technology", "market_cap": 5e12},
        {"symbol": "SPY", "name": "SPDR S&P 500", "kind": "etf"},
        {"symbol": "SPX", "name": "S&P 500 Index", "kind": "index"},
        {"symbol": "AAP", "name": "Advance Auto Parts", "kind": "stock", "market_cap": 3e9},
    ])
    symbols.upsert_symbols([{"symbol": "AAPL", "kind": "stock", "name": None, "market_cap": 5.1e12}])
    symbols.upsert_symbols([{"symbol": "DELL", "name": "Dell", "kind": "stock", "industry": "Computers",
                             "market_cap": 1e11}])
    symbols.upsert_symbols([{"symbol": "AAPL", "kind": "stock", "industry": "Computers"}])
    assert [r["symbol"] for r in symbols.by_industry("Computers")] == ["AAPL", "DELL"]
    assert [r["symbol"] for r in symbols.by_industry("Computers", limit=1)] == ["AAPL"]
    a = symbols.get_symbol("AAPL")
    assert a["name"] == "Apple Inc." and a["sector"] == "Technology" and a["market_cap"] == 5.1e12
    assert a["optionable"] is None and a["in_universe"] is False and a["updated_at"].tzinfo is not None
    assert symbols.set_universe(["AAPL", "SPY", "SPX"]) == 3
    assert symbols.set_universe(["AAPL", "SPY"]) == 2
    symbols.set_optionable("SPY", False)
    assert symbols.universe_symbols() == ["AAPL"]
    assert symbols.universe_symbols(include_unoptionable=True) == ["AAPL", "SPY"]
    symbols.reprobe_not_optionable(["SPY"])
    assert symbols.get_symbol("SPY")["optionable"] is None
    symbols.set_optionable("AAPL", True)
    assert symbols.counts() == {"universe_size": 2, "optionable_count": 1, "total": 5}
    assert [r["symbol"] for r in symbols.search_symbols("aap")] == ["AAP", "AAPL"]  # exact match first
    assert [r["symbol"] for r in symbols.search_symbols("aa")] == ["AAPL", "AAP"]   # then universe
    assert symbols.search_symbols("AAPL")[0]["symbol"] == "AAPL"
    assert [r["symbol"] for r in symbols.search_symbols("s&p")] == ["SPY", "SPX"]
    assert [r["symbol"] for r in symbols.list_symbols(kind="index")] == ["SPX"]


# ----------------------------------------------------------------------------- bars
def _bars(sym: str, n: int, start=date(2026, 1, 1), close0=100.0) -> list[Bar]:
    return [Bar(sym, start + timedelta(days=i), close0 + i, close0 + i + 1, close0 + i - 1, close0 + i, 1000 + i)
            for i in range(n)]


def test_bars_roundtrip(tmpdb):
    assert bars.upsert_bars(_bars("AAA", 30) + _bars("BBB", 5)) == 35
    bars.upsert_bars([Bar("AAA", date(2026, 1, 1), None, None, None, 99.0, None)])
    b = bars.get_bars("AAA")
    assert len(b["date"]) == 30 and b["date"].dtype == np.dtype("datetime64[D]")
    assert b["close"][0] == 99.0 and np.isnan(b["open"][0]) and np.isnan(b["volume"][0])
    b2 = bars.get_bars("AAA", start=date(2026, 1, 10), end=date(2026, 1, 12))
    assert list(b2["close"]) == [109.0, 110.0, 111.0]
    bars.upsert_bars([Bar("AAA", date(2026, 1, 2), 1, 1, 1, 1.0, 1)], replace=False)
    assert bars.get_bars("AAA")["close"][1] == 101.0
    lc = bars.last_closes(3)
    assert set(lc) == {"AAA", "BBB"} and list(lc["AAA"]["close"]) == [127.0, 128.0, 129.0]
    since = bars.bars_since(date(2026, 1, 29), ["AAA"])
    assert list(since) == ["AAA"] and len(since["AAA"]["date"]) == 2
    cov = bars.coverage()
    assert cov["BBB"] == {"count": 5, "first": date(2026, 1, 1), "last": date(2026, 1, 5)}
    assert bars.replace_bars("BBB", _bars("BBB", 2, close0=50)) == 2
    assert list(bars.get_bars("BBB")["close"]) == [50.0, 51.0]
    assert bars.bars_on(date(2026, 1, 2))["BBB"].close == 51.0


# ----------------------------------------------------------------------------- quotes + options
def test_option_latest_and_eod(tmpdb, aapl):
    quotes.upsert_quotes([aapl.quote])
    assert options.replace_option_latest(aapl) == 3386
    assert options.replace_option_latest(aapl) == 3386       # replace, not append
    assert status.row_counts()["option_latest"] == 3386
    q = quotes.get_quote("AAPL")
    assert q == replace(aapl.quote, ts=aapl.quote.ts.astimezone(UTC))
    chain = options.get_chain("AAPL")
    assert len(chain.contracts) == 3386 and chain.fetched_at == aapl.fetched_at
    by_osi = {c.osi: c for c in aapl.contracts}
    assert all(by_osi[c.osi] == c for c in chain.contracts)
    exps = options.expiries("AAPL")
    assert exps[0] == date(2026, 11, 20) and exps == sorted(exps)
    one = options.get_chain("AAPL", expiry=exps[0])
    assert {c.expiry for c in one.contracts} == {exps[0]}
    assert options.latest_fetched_at("AAPL") == aapl.fetched_at and options.latest_fetched_at("X") is None

    cols = options.latest_columns(right="P", expiry_min=date(2026, 12, 1), expiry_max=date(2027, 1, 31), min_oi=100)
    assert len(cols["osi"]) > 0 and set(cols["right"]) == {"P"}
    assert (cols["oi"] >= 100).all() and np.all(cols["underlying"] == aapl.quote.price)
    assert cols["expiry"].min() >= np.datetime64("2026-12-01")

    day = date(2026, 11, 19)
    n = options.write_option_eod(day, datetime(2026, 11, 19, 21, 0, tzinfo=UTC))
    empty = sum(1 for c in aapl.contracts if not c.bid and not c.oi and not c.volume)
    assert n == 3386 - empty and n > 0
    assert options.write_option_eod(day, datetime(2026, 11, 19, 21, 0, tzinfo=UTC)) == n  # idempotent
    assert status.row_counts()["option_eod"] == n
    assert options.write_option_eod(day, datetime(2026, 11, 19, 22, 0, tzinfo=UTC)) == 0  # stale data skipped
    assert status.row_counts()["option_eod"] == n
    e = options.eod_for_date(day, symbols=["AAPL"], right="C")
    assert set(e["right"]) == {"C"} and np.all(e["underlying"] == aapl.quote.price)
    osi = e["osi"][0]
    assert len(options.eod_for_osi(osi)["date"]) == 1
    assert len(options.eod_for_symbol("AAPL", day, day)["osi"]) == n
    assert options.eod_dates() == [day] and status.distinct_dates("option_eod") == 1
    assert options.prior_oi(date(2026, 11, 20), "AAPL")[osi] == by_osi[osi].oi
    assert options.prior_oi(day) == {}
    # a later EOD day stored for another symbol only: AAPL still compares with its own latest day
    with db.write() as cur:
        cur.execute("INSERT INTO option_eod (date, symbol, osi, expiry, strike, \"right\", oi) "
                    "VALUES ('2026-11-20', 'MSFT', 'MSFT261218C00500000', '2026-12-18', 500, 'C', 7)")
    assert options.prior_eod_date(date(2026, 11, 23)) == date(2026, 11, 20)          # global latest
    assert options.prior_eod_date(date(2026, 11, 23), "AAPL") == day
    assert options.prior_oi(date(2026, 11, 23), "AAPL")[osi] == by_osi[osi].oi       # not {} (no fake change)
    assert options.prior_eod_date(date(2026, 11, 20), "MSFT") is None
    db.cursor().execute("DELETE FROM option_eod WHERE symbol = 'MSFT'")
    assert options.prune_option_eod(date(2026, 11, 20)) == n


def test_option_latest_bulk_write_is_fast(tmpdb, aapl):
    # SPX-sized chain (~20k contracts) must be written in well under a second.
    many = []
    for k in range(6):
        many += [replace(c, osi=c.osi.replace("AAPL", f"AAP{k}")) for c in aapl.contracts]
    snap = replace(aapl, contracts=tuple(many))
    t0 = time.perf_counter()
    assert options.replace_option_latest(snap) == len(many)
    assert time.perf_counter() - t0 < 1.5


# ----------------------------------------------------------------------------- metrics
def test_metrics_upsert_and_reads(tmpdb):
    d0 = date(2026, 1, 1)
    for i in range(30):
        metrics.upsert_metrics(d0 + timedelta(days=i), "AAA", {"iv30": 0.2 + i / 100, "call_volume": 10 * i})
        metrics.upsert_metrics(d0 + timedelta(days=i), "BBB", {"iv30": 0.5})
    metrics.upsert_metrics(d0, "AAA", {"iv30": float("nan"), "hv20": np.float64(0.3)})
    with pytest.raises(ValueError):
        metrics.upsert_metrics(d0, "AAA", {"not_a_column": 1})
    assert "iv30" in metrics.metric_columns() and "updated_at" not in metrics.metric_columns()
    r = metrics.get_metrics("AAA", d0)
    assert r["iv30"] is None and r["hv20"] == 0.3 and r["call_volume"] is None
    assert metrics.get_metrics("AAA")["date"] == d0 + timedelta(days=29)
    assert metrics.latest_date() == d0 + timedelta(days=29)
    assert set(metrics.metrics_on()) == {"AAA", "BBB"}
    h = metrics.history("AAA", cols=["iv30", "call_volume"])
    assert len(h["date"]) == 30 and np.isnan(h["iv30"][0]) and h["call_volume"][-1] == 290
    hb = metrics.history_bulk(["iv30"], last_n=5)
    assert len(hb["AAA"]["iv30"]) == 5 and hb["AAA"]["iv30"][-1] == pytest.approx(0.49)
    s = metrics.series_between(d0, d0 + timedelta(days=1), ["iv30"])
    assert list(s["symbol"]) == ["AAA", "BBB", "AAA", "BBB"]
    assert len(metrics.dates()) == 30 and status.distinct_dates("metrics_daily") == 30
    with pytest.raises(ValueError):
        metrics.history("AAA", cols=["iv30; DROP TABLE x"])


# ----------------------------------------------------------------------------- earnings / dividends
def _ev(sym, d, eps=None):
    return EarningsEvent(sym, d, "amc", "Sep/2026", eps, 1.0, None, 5)


def test_earnings(tmpdb):
    d1, d2 = date(2026, 11, 20), date(2026, 12, 1)
    earnings.replace_day(d1, [_ev("AAA", d1), _ev("BBB", d1)])
    earnings.replace_day(d1, [_ev("AAA", d1, eps=1.1)])        # BBB rescheduled away
    earnings.upsert_earnings([_ev("BBB", d2), _ev("AAA", date(2026, 8, 1), 0.9)])
    assert [e.symbol for e in earnings.between(d1, d1)] == ["AAA"]
    assert earnings.between(d1, d1)[0].eps_actual == 1.1
    assert [e.date for e in earnings.for_symbol("AAA")] == [date(2026, 8, 1), d1]
    nxt = earnings.next_events(date(2026, 11, 1))
    assert nxt["AAA"].date == d1 and nxt["BBB"].date == d2
    assert earnings.last_events(d1)["AAA"].date == date(2026, 8, 1)
    assert earnings.between(d1, d2, symbols=["BBB"])[0].symbol == "BBB"


def test_dividends(tmpdb):
    divs = [Dividend("KO", date(2026, m, 15), 0.51, None, None, date(2026, m, 1), "cash") for m in (3, 6, 9, 11)]
    dividends.upsert_dividends(divs + [Dividend("KO", date(2026, 11, 15), 0.52, None, None, None, "cash")])
    assert [d.amount for d in dividends.for_symbol("KO")] == [0.51, 0.51, 0.51, 0.52]
    assert dividends.trailing_sum(date(2026, 11, 30))["KO"] == pytest.approx(2.05)
    assert dividends.next_ex(date(2026, 7, 1))["KO"].ex_date == date(2026, 9, 15)
    assert len(dividends.between(date(2026, 6, 1), date(2026, 9, 1), by="declared_date")) == 2
    with pytest.raises(ValueError):
        dividends.between(date(2026, 1, 1), date(2026, 2, 1), by="bogus")  # type: ignore[arg-type]
    assert dividends.symbols_with_history() == {"KO"}
    first = min(d.ex_date for d in dividends.for_symbol("KO"))
    assert dividends.symbols_with_history(covering=first) == {"KO"}
    assert dividends.symbols_with_history(covering=first - timedelta(days=1)) == set()


# ----------------------------------------------------------------------------- market
def test_index_bars_and_rates(tmpdb):
    market.upsert_index_bars([Bar("VIX", date(2026, 11, d), 20, 21, 19, 20 + d / 10, None) for d in (17, 18)])
    ib = market.index_bars(["VIX", "VIX3M"])
    assert list(ib) == ["VIX"] and ib["VIX"]["close"][-1] == pytest.approx(21.8)
    assert market.index_last_date("VIX") == date(2026, 11, 18)
    assert market.rate_on() is None
    market.upsert_rates([(date(2026, 11, 17), 0.0425), (date(2026, 11, 18), 0.0426)])
    assert market.rate_on() == 0.0426 and market.rate_on(date(2026, 11, 17)) == 0.0425
    assert market.rate_on(date(2026, 1, 1)) is None
    assert len(market.rates_between(date(2026, 11, 1), date(2026, 11, 30))) == 2


# ----------------------------------------------------------------------------- user
def test_watchlists(tmpdb):
    w = user.create_watchlist("Tech", ["AAPL", "MSFT", "AAPL"])
    assert w["name"] == "Tech" and w["symbols"] == ["AAPL", "MSFT"] and w["created_at"].tzinfo
    with pytest.raises(ValueError):
        user.create_watchlist("Tech")
    e = user.create_watchlist("Empty")
    assert e["symbols"] == []
    assert user.add_symbol(w["id"], "NVDA") and not user.add_symbol(999, "X")
    assert user.remove_symbol(w["id"], "MSFT") and not user.remove_symbol(w["id"], "MSFT")
    assert user.get_watchlist(w["id"])["symbols"] == ["AAPL", "NVDA"]
    assert user.update_watchlist(w["id"], name="Tech2", symbols=["TSLA"])["symbols"] == ["TSLA"]
    assert user.update_watchlist(999, name="x") is None
    assert user.watchlist_symbols() == ["TSLA"]
    assert user.delete_watchlist(e["id"]) and not user.delete_watchlist(e["id"])
    assert [x["name"] for x in user.list_watchlists()] == ["Tech2"]


def test_presets_alerts_notifications(tmpdb):
    p = user.save_preset("iv-rankings", "high", {"params": {"x": 1}, "filters": {}})
    p2 = user.save_preset("iv-rankings", "high", {"params": {"x": 2}, "filters": {}})
    assert p2["id"] == p["id"] and p2["payload"]["params"]["x"] == 2
    assert len(user.list_presets("iv-rankings")) == 1 and user.list_presets("other") == []
    assert user.delete_preset(p["id"]) and user.list_presets() == []

    a = user.create_alert("iv30_above", "AAPL", {"value": 0.4})
    assert a["enabled"] is True and a["cooldown_min"] == 240 and a["params"] == {"value": 0.4}
    assert user.update_alert(a["id"], enabled=False, params={"value": 0.5})["params"] == {"value": 0.5}
    with pytest.raises(ValueError):
        user.update_alert(a["id"], bogus=1)
    assert user.list_alerts(enabled=True) == [] and len(user.list_alerts()) == 1
    t = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
    user.record_alert_fire(a["id"], "AAPL", t)
    assert user.alert_fired_at(a["id"]) == {"AAPL": t} and user.get_alert(a["id"])["last_fired_at"] == t

    n1 = user.add_notification("t1", "b1", alert_id=a["id"], ts=t)
    n2 = user.add_notification("t2", "b2", link="/s/AAPL")
    assert user.unread_count() == 2
    assert [n["id"] for n in user.list_notifications()] == [n2, n1]
    assert user.mark_read([n1]) == 1 and user.unread_count() == 1
    assert user.mark_read() == 1 and user.list_notifications(unread_only=True) == []
    assert user.prune_notifications(datetime(2026, 11, 19, 16, 0, tzinfo=UTC)) == 1
    assert user.delete_alert(a["id"]) and user.get_alert(a["id"]) is None


# ----------------------------------------------------------------------------- cache / jobs / snapshots
def test_cache_jobs_snapshots_status(tmpdb):
    cache.cache_put("news:AAPL", [{"title": "x", "created": date(2026, 11, 19)}])
    assert cache.cache_get("news:AAPL", 60) == [{"title": "x", "created": "2026-11-19"}]
    assert cache.cache_get("missing", 60) is None
    time.sleep(0.01)
    assert cache.cache_get("news:AAPL", 0.001) is None and cache.cache_get_any("news:AAPL")[0][0]["title"] == "x"

    rid = jobs.start_run("sweep")
    jobs.update_run(rid, "50/100")
    assert jobs.latest_run("sweep")["detail"] == "50/100" and jobs.latest_run("sweep")["ok"] is None
    jobs.finish_run(rid, True)
    r = jobs.latest_run("sweep", ok=True)
    assert r["detail"] == "50/100" and r["finished_at"] >= r["started_at"]
    jobs.finish_run(jobs.start_run("eod"), False, "boom")
    assert jobs.latest_per_job()["eod"]["detail"] == "boom" and len(jobs.recent_runs()) == 2
    assert jobs.prune_runs(datetime.now(UTC) + timedelta(days=1)) == 0   # latest per job kept

    d = date(2026, 11, 19)
    assert report_snapshots.write_snapshots(d, "unusual-option-volume", [{"symbol": "AAPL", "rel": 3.1}]) == 1
    report_snapshots.write_snapshots(d, "unusual-option-volume", [{"symbol": "TSLA", "rel": 2.0}])
    snaps = report_snapshots.read_snapshots("unusual-option-volume", d)
    assert snaps == [{"date": d, "symbol": "TSLA", "payload": {"symbol": "TSLA", "rel": 2.0}}]
    assert report_snapshots.snapshot_dates("unusual-option-volume") == [d]

    counts = status.row_counts()
    assert counts["http_cache"] == 1 and counts["job_runs"] == 2 and set(counts) == set(status.TABLES)
    assert status.db_size_bytes() > 0


def test_earnings_keep_known_timing_and_estimates(tmpdb):
    d = date(2026, 11, 18)
    earnings.replace_day(d, [EarningsEvent("PANW", d, "amc", "Oct/2026", None, 0.52, None, 20)])
    # once the date has passed Nasdaq reports actuals but no time / estimate count
    earnings.replace_day(d, [EarningsEvent("PANW", d, "unknown", None, 0.55, 0.52, 0.0577, None)])
    e = earnings.for_symbol("PANW")[0]
    assert (e.timing, e.fiscal_quarter, e.eps_actual, e.num_estimates) == ("amc", "Oct/2026", 0.55, 20)
    earnings.upsert_earnings([EarningsEvent("PANW", d, "bmo", None, None, None, None, None)])
    e = earnings.for_symbol("PANW")[0]
    assert (e.timing, e.eps_actual, e.surprise_pct) == ("bmo", 0.55, 0.0577)


def test_option_latest_per_symbol_bulk_reads(tmpdb, aapl):
    quotes.upsert_quotes([aapl.quote])
    options.replace_option_latest(aapl)
    by = options.latest_by_symbol(right="C", min_oi=10)
    assert list(by) == ["AAPL"] and "symbol" not in by["AAPL"]
    assert set(by["AAPL"]["right"]) == {"C"} and (by["AAPL"]["oi"] >= 10).all()
    near = options.near_atm_by_symbol(date(2026, 12, 1), date(2027, 6, 30), 0.05)["AAPL"]
    spot = aapl.quote.price
    assert len(near["osi"]) > 0 and np.all(np.abs(near["strike"] / spot - 1) <= 0.05)
    assert near["expiry"].min() >= np.datetime64("2026-12-01") and set(near["right"]) == {"C", "P"}
    assert options.near_atm_by_symbol(date(2026, 12, 1), date(2027, 6, 30), 0.05, symbols=["X"]) == {}


def test_watchlist_symbols_keep_insertion_order(tmpdb):
    w = user.create_watchlist("Order", ["TSLA", "AAPL", "MSFT"])
    assert w["symbols"] == ["TSLA", "AAPL", "MSFT"]
    assert user.update_watchlist(w["id"], symbols=["KO", "ZM", "AAPL", "BA"])["symbols"] == ["KO", "ZM", "AAPL", "BA"]
    user.add_symbol(w["id"], "AMD")
    assert user.get_watchlist(w["id"])["symbols"] == ["KO", "ZM", "AAPL", "BA", "AMD"]
