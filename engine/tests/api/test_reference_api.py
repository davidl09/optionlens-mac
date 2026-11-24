"""Gaps filled from Questrade and IBKR (docs/INTEGRATION.md §6 "Dividends", §8 "Reference data", "Extended hours"):
one source of the fundamental figures across the symbol header, Summary, peers and the screeners; dividends of
non-Nasdaq listings from IBKR's adjusted closes and Questrade's current dividend; VWAP and halts in quotes;
extended-hours movers of the whole universe with the Nasdaq fallback."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone

import pytest

from optionlens.analytics import calendar as cal
from optionlens.analytics.calendar import NY
from optionlens.jobs import extended as extended_job
from optionlens.models import Dividend, ExtendedQuote, Fundamentals, Quote
from optionlens.repo import dividends as div_repo
from optionlens.repo import fundamentals as fund_repo
from optionlens.repo import quotes as quotes_repo
from optionlens.services import reference

from ..test_dividend_sources import ibkr_adjusted, quarterly_ex_dates, series
from .seed_reports import _clear_caches, api, seeded  # noqa: F401 - fixtures
from .tsschema import load_types, mismatches

NOW = datetime.now(timezone.utc)


def _snap(sym: str, day: date, **kw) -> Fundamentals:
    base = dict(pe=31.0, eps=7.0, market_cap=3.1e12, shares_outstanding=15.0e9, high_52w=290.0, low_52w=160.0,
                avg_volume_20d=5.1e7, avg_volume_3m=4.8e7, sector="Technology", industry_group="Hardware",
                industry_subgroup="Computers", dividend_yield=0.0044, prev_close=210.0)
    return Fundamentals(sym, day, "questrade", **{**base, **kw})


@pytest.fixture(autouse=True)
def _no_visit_fetches(monkeypatch):
    """Opening a symbol page records the visit and fetches Nasdaq company data (network disabled here)."""
    from optionlens.jobs import ondemand

    async def visit(symbol):
        return None
    monkeypatch.setattr(ondemand, "visit", visit)


@pytest.fixture(scope="module")
def with_reference(seeded, api):
    """AAPL with today's Questrade reference snapshot (stored through services.reference) and a live VWAP quote."""
    today = seeded["today"]
    ref = {"AAPL": (_snap("AAPL", today), Dividend("AAPL", today + timedelta(days=40), 0.27, today + timedelta(days=50),
                                                    None, None, "cash", source="questrade"))}
    assert reference.store(ref, today) == 1 and reference.store(ref, today) == 0     # once per symbol per day
    q = quotes_repo.get_quote("AAPL")
    from dataclasses import replace
    quotes_repo.upsert_quotes([replace(q, vwap=round(q.price * 0.99, 2))], "questrade")
    _clear_caches()
    return {"price": q.price, "vwap": round(q.price * 0.99, 2)}


def test_key_stats_are_one_value_on_every_page(api, with_reference):
    px = with_reference["price"]
    ks = api.get("/api/symbol/AAPL/key-stats").json()
    assert mismatches(ks, load_types().parse("KeyStats")) == []
    assert ks["pe"] == pytest.approx(px / 7.0) and ks["market_cap"] == pytest.approx(px * 15.0e9)   # current price
    assert (ks["eps"], ks["high_52w"], ks["avg_volume_3m"], ks["industry_subgroup"]) == (7.0, 290.0, 4.8e7, "Computers")
    assert ks["sources"]["pe"] == "questrade" and ks["sources"]["market_cap"] == "questrade"
    summary = api.get("/api/symbol/AAPL/summary").json()
    assert summary["key_stats"] == ks and summary["market_cap"] == ks["market_cap"]
    assert (summary["high_52w"], summary["low_52w"]) == (290.0, 160.0)               # Questrade's 52-week range
    screener = {r["symbol"]: r for r in api.get("/api/reports/stock-screener").json()["rows"]}
    assert screener["AAPL"]["market_cap"] == pytest.approx(ks["market_cap"])
    assert screener["AAPL"]["pe"] == pytest.approx(ks["pe"]) and screener["AAPL"]["fundamentals_source"] == "questrade"
    assert screener["AAPL"]["div_yield"] == pytest.approx(ks["dividend_yield"])
    peers = {r["symbol"]: r for r in api.get("/api/symbol/AAPL/peers").json()["rows"]}
    assert peers["AAPL"]["market_cap"] == pytest.approx(ks["market_cap"]) and peers["AAPL"]["pe"] == pytest.approx(ks["pe"])
    # the dividend yield: the indicated yield of the merged dividends, the same on the Dividends tab and the Summary
    divs = api.get("/api/symbol/AAPL/dividends").json()
    assert divs["yield"] == pytest.approx(ks["dividend_yield"]) and ks["sources"]["dividend_yield"] == "nasdaq"
    assert summary["next_dividend"]["yield"] == pytest.approx(ks["dividend_yield"])
    # XYZ has no snapshot: only the market cap is known (Nasdaq's), the rest stays empty, never invented
    xyz = api.get("/api/symbol/XYZ/key-stats").json()
    assert xyz["market_cap"] == 8e10 and xyz["sources"]["market_cap"] == "nasdaq" and xyz["pe"] is None
    assert xyz["asof"] is None and screener["XYZ"]["pe"] is None


def test_fundamentals_tab_does_not_repeat_the_own_figures(api, with_reference, monkeypatch):
    from optionlens.services import symbol_company
    nasdaq = {"Exchange": "NASDAQ-GS", "MarketCap": "$3,000,000,000,000", "PERatio": "29.1", "EarningsPerShare": "$7.1",
              "Yield": "0.5%", "FiftTwoWeekHighLow": "$288 / $150", "Beta": "1.2"}
    monkeypatch.setattr(symbol_company, "_cached", lambda kind, sym, **p: (nasdaq if kind == "summary" else None, None))
    f = api.get("/api/symbol/AAPL/fundamentals").json()
    assert [k["label"] for k in f["key_stats"]] == ["Exchange", "Beta"]              # Nasdaq's other figures only


def test_questrade_current_dividend_fills_the_merged_set(api, with_reference, seeded):
    hist = api.get("/api/symbol/AAPL/dividends").json()["history"]
    q = next(h for h in hist if h["source"] == "questrade")
    assert q["amount"] == 0.27 and q["pay_date"] == (seeded["today"] + timedelta(days=50)).isoformat()
    assert {h["source"] for h in hist} == {"nasdaq", "questrade"}


def test_vwap_and_halt_reach_the_quote_and_the_screener(api, with_reference):
    from optionlens.repo import halts
    halts.record([("AAPL", True, datetime.now(timezone.utc))], "questrade")
    _clear_caches()
    quote = api.get("/api/symbol/AAPL/summary").json()["quote"]
    assert quote["vwap"] == with_reference["vwap"] and quote["halted"] is True
    row = {r["symbol"]: r for r in api.get("/api/reports/market-movers?side=active").json()["rows"]}["AAPL"]
    assert row["vwap"] == with_reference["vwap"] and row["halted"] is True
    assert row["pct_vs_vwap"] == pytest.approx(with_reference["price"] / with_reference["vwap"] - 1)
    assert api.get("/api/symbol/XYZ/summary").json()["quote"]["halted"] is None      # never observed: unknown
    halts.record([("AAPL", False, datetime.now(timezone.utc))], "questrade")
    _clear_caches()


def test_dividend_history_from_ibkr_adjusted_closes(api, seeded):
    """SPY (no Nasdaq history): wanted for ten years; the pushed closes give its dividends, the fetch is recorded and
    the symbol is not wanted again for a week. For AAPL a derived amount that differs from Nasdaq's is flagged."""
    w = api.get("/api/ingest/wants").json()["dividend_history"]
    want = {x["symbol"]: x["from"] for x in w}
    assert set(want) == {"AAPL", "XYZ", "SPY"} and want["SPY"].startswith(str(NOW.year - 10))
    end = seeded["prev"]
    raw = series(end - timedelta(days=800), end, first=600.0)
    ex = quarterly_ex_dates(raw[0][0], end)
    adj = ibkr_adjusted(raw, {d: 1.75 for d in ex})
    body = {"source": "ibkr", "symbol": "SPY", "trades": [{"date": d.isoformat(), "close": c} for d, c in raw],
            "adjusted": [{"date": d.isoformat(), "close": a} for d, a in adj]}
    assert api.post("/api/ingest/dividend-history", json={**body, "trades": [{"date": "x", "close": 1}]}).status_code == 422
    r = api.post("/api/ingest/dividend-history", json=body)
    assert r.status_code == 200 and r.json() == {"stored": len(ex), "symbol": "SPY"}
    d = api.get("/api/symbol/SPY/dividends").json()
    assert {h["source"] for h in d["history"]} == {"ibkr-adjusted"} and len(d["history"]) == len(ex)
    assert d["annual_rate"] == pytest.approx(4 * 1.75, abs=1e-3) and d["frequency"] == 4
    assert "SPY" not in {x["symbol"] for x in api.get("/api/ingest/wants").json()["dividend_history"]}
    # AAPL: Nasdaq stays the amount; a derived one $0.05 higher on a Nasdaq ex-date flags the row
    nasdaq = [x for x in div_repo.for_symbol("AAPL") if x.source == "nasdaq" and x.ex_date <= end][-1]
    div_repo.upsert_derived([Dividend("AAPL", nasdaq.ex_date, nasdaq.amount + 0.05, None, None, None, "cash",
                                      source="ibkr-adjusted")], NOW)
    row = next(h for h in api.get("/api/symbol/AAPL/dividends").json()["history"] if h["ex_date"] == nasdaq.ex_date.isoformat())
    assert row["source"] == "nasdaq" and row["amount"] == nasdaq.amount and row["discrepancy"] is True
    cal_rows = api.get(f"/api/reports/dividend-calendar?start={nasdaq.ex_date}&end={nasdaq.ex_date}").json()["rows"]
    assert {(r["symbol"], r["source"], r["discrepancy"]) for r in cal_rows} >= {("AAPL", "nasdaq", True)}


def test_dividend_history_is_parsed_strictly_and_empty_series_are_recorded(api):
    assert api.post("/api/ingest/dividend-history", json={"source": "ibkr", "symbol": "XYZ", "adjusted": [],
                                                         "trades": []}).json() == {"stored": 0, "symbol": "XYZ"}
    assert div_repo.history_fetches(["XYZ"])["XYZ"]["last_date"] is None   # asked again in a week, from 10y back


def test_extended_movers_cover_every_symbol_questrade_quoted_with_the_nasdaq_fallback(api, seeded):
    day = seeded["day"]                                                    # the latest session (a trading day)
    at = datetime.combine(day, time(8, 0), NY)
    quotes = [ExtendedQuote("AAPL", at, 212.0, 200.0, 1_200_000, at - timedelta(minutes=3), False, False),
              ExtendedQuote("XYZ", at, 50.0, 50.0, 1000, datetime.combine(day - timedelta(days=1), time(15, 59), NY),
                            False, False)]   # XYZ: no pre-market trade
    n, changed = extended_job.store("pre", at, quotes, "questrade")
    assert n == 2 and changed == []
    r = api.get("/api/reports/premarket-movers").json()
    rows = {x["symbol"]: x for x in r["rows"]}
    a = rows["AAPL"]
    assert (a["source"], a["ext_price"], a["ext_change"], a["day_volume"]) == ("questrade", 212.0, 12.0, 1_200_000)
    assert a["ext_change_pct"] == pytest.approx(0.06) and a["ext_updated"] == "07:57 ET"
    assert rows["SPY"]["source"] == "nasdaq"                       # no Questrade quote: Nasdaq (watchlists ∪ top N)
    assert rows["XYZ"]["source"] == "nasdaq"                       # Questrade saw no pre-market trade
    assert "1 symbols from Questrade" in r["notes"][0]
    assert api.get("/api/reports/afterhours-movers").json()["rows"][0]["source"] == "nasdaq"   # none collected


def test_extended_collection_only_in_the_windows(monkeypatch, seeded):
    calls = []

    class Src:
        name = "questrade"

        def unavailable(self):
            return None

        async def fetch_extended(self, symbols):
            calls.append(symbols)
            at = datetime.now(timezone.utc)
            return [ExtendedQuote("XYZ", at, 51.0, 50.0, 10, at, True, False)]

    monkeypatch.setattr(extended_job, "_sources", lambda: [Src()])
    fired = []
    monkeypatch.setattr(extended_job.hooks, "on_halt_change", [lambda d, s: fired.append(s)])
    day = cal.next_trading_day(date(2026, 11, 26))                                 # a trading day
    assert asyncio.run(extended_job.collect(datetime.combine(day, time(12, 0), NY))).startswith("skipped")
    msg = asyncio.run(extended_job.collect(datetime.combine(day, time(17, 0), NY)))
    assert msg == f"questrade: 1 post-market quotes of {len(calls[0])} symbols" and {"AAPL", "XYZ"} <= set(calls[0])
    assert fired == [["XYZ"]]                                                       # XYZ halted: alerts evaluate now


def test_watchlist_table_shows_the_halt_and_vwap_columns(api, with_reference, seeded):
    t = api.get(f"/api/watchlists/{seeded['watchlist_id']}/table").json()
    keys = [c["key"] for c in t["columns"]]
    assert "halted" in keys and "pct_vs_vwap" in keys
    assert {c["key"]: c["fmt"] for c in t["columns"]}["halted"] == "halt"


def test_reference_hook_registers_once_and_reads_today_from_every_source(monkeypatch, seeded):
    from optionlens.jobs import hooks
    before = list(hooks.on_sweep_complete)
    try:
        reference.register_hooks()
        reference.register_hooks()
        assert hooks.on_sweep_complete.count(reference.store_reference) == 1
    finally:
        hooks.on_sweep_complete[:] = before
    asked = []

    class Src:
        name = "questrade"

        def cached_reference(self, day):
            asked.append(day)
            return {"XYZ": (_snap("XYZ", day, eps=2.0), None)}

    monkeypatch.setattr(reference, "_sources", lambda: [Src()])
    assert reference.store_reference(date(2000, 1, 3), ["XYZ"]) == 1                  # the hook's day is ignored
    assert asked == [seeded["today"]] and fund_repo.latest(["XYZ"])["XYZ"].eps == 2.0
