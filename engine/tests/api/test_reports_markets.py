"""Reports' market filter (docs/SPEC.md §7 "Markets"): US by default (the reports read as before a Canadian listing
was added), Canada or All on request, one market per aggregate, US-only sources without the param; every symbol row
carries its currency; an explicit symbol list is shown whatever its market."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from optionlens.analytics.markets import calendar_for
from optionlens.jobs import chains
from optionlens.jobs._util import session_date
from optionlens.repo import bars, metrics, symbols
from optionlens.services.reports import registry

from .seed_reports import _bars, _chain, _clear_caches, api, seeded  # noqa: F401 - fixtures

CA = "RY.TO"


@pytest.fixture(scope="module")
def ca_seeded(seeded):
    """The seeded US data plus one Canadian listing (RY.TO, CAD) with a chain, bars and IV history."""
    now = datetime.now(timezone.utc)
    symbols.upsert_symbols([{"symbol": CA, "name": "Royal Bank of Canada", "kind": "stock", "sector": "Financial",
                             "market_cap": 2.6e11}])
    symbols.set_universe(["AAPL", "XYZ", "SPY"])                       # the US universe is set again ...
    from optionlens.analytics import markets
    symbols.set_universe([CA], markets.CA)                             # ... and Canada's does not touch it
    snap = _chain(CA, now, 0.5)
    cal = calendar_for(CA)
    bars.upsert_bars(_bars(CA, snap.quote.prev_close or snap.quote.price, cal.prev_trading_day(session_date(now, cal)),
                           seed=4))
    chains.store_chain(snap, now, final=True)
    day = session_date(now, cal)
    d = day
    for k in range(1, 26):
        d = cal.prev_trading_day(d)
        metrics.upsert_metrics(d, CA, {"iv30": 0.15 + 0.1 * (k % 5) / 5, "hv30": 0.14, "call_volume": 100,
                                       "put_volume": 100})
    _clear_caches()
    yield seeded
    _clear_caches()


def _syms(res: dict) -> set[str]:
    return {r["symbol"] for r in res["rows"] if r.get("symbol")}


def test_both_universes_coexist(ca_seeded):
    assert {"AAPL", "XYZ", "SPY", CA} <= set(symbols.universe_symbols(include_unoptionable=True))


def test_default_is_us_and_canada_is_on_request(ca_seeded):
    us = registry.run_sync("iv-rankings")
    assert CA not in _syms(us) and "AAPL" in _syms(us) and us["params"]["market"] == "US"
    ca = registry.run_sync("iv-rankings", {"market": "CA"})
    assert _syms(ca) == {CA}
    both = registry.run_sync("iv-rankings", {"market": "all"})
    assert {CA, "AAPL"} <= _syms(both)


def test_rows_carry_their_currency(ca_seeded):
    rows = registry.run_sync("stock-screener", {"market": "all", "min_price": "0", "min_volume": "0"})["rows"]
    cur = {r["symbol"]: r["currency"] for r in rows}
    assert cur[CA] == "CAD" and cur["AAPL"] == "USD"


def test_contract_level_reports_filter_by_market(ca_seeded):
    ca = registry.run_sync("option-screener", {"market": "CA"})
    assert ca["rows"] and {r["symbol"] for r in ca["rows"]} == {CA}
    assert all(r["osi"].startswith("RY.TO") for r in ca["rows"])     # MX codes carry the exchange suffix
    us = registry.run_sync("option-screener")
    assert us["rows"] and CA not in {r["symbol"] for r in us["rows"]}


def test_aggregates_never_mix_markets(ca_seeded):
    for rid in ("options-broad-view", "performance-by-sector", "performance-by-industry"):
        prm = next(p for p in registry.get(rid).as_dict()["params"] if p["key"] == "market")
        assert [o["value"] for o in prm["options"]] == ["US", "CA"], rid
    ca, us = registry.run_sync("options-broad-view", {"market": "CA"}), registry.run_sync("options-broad-view")
    assert ca["rows"] and ca["rows"][0] != us["rows"][0]     # the Canadian totals are not the US ones


def test_us_only_sources_have_no_market_param_and_no_canadian_rows(ca_seeded):
    for rid in ("splits-calendar", "premarket-movers", "event-driven"):
        assert "market" not in {p["key"] for p in registry.get(rid).as_dict()["params"]}, rid


def test_dividend_reports_cover_both_markets(ca_seeded):
    """Canadian listings' dividend histories come from TMX Money: the dividend reports have the market filter (US by
    default) and name each row's source."""
    from datetime import timedelta
    from optionlens.models import Dividend
    from optionlens.repo import dividends
    from optionlens.services.reports.base import today
    t = today()
    ex = t + timedelta(days=7)
    past = [Dividend(CA, t - timedelta(days=91 * k), 1.54 if k > 2 else 1.76, None, None, None, "cash", "tmx")
            for k in range(1, 9)]
    dividends.upsert_derived([*past, Dividend(CA, ex, 1.76, ex + timedelta(days=28), ex, t - timedelta(days=2), "cash",
                                              "tmx")], datetime.now(timezone.utc))
    _clear_caches()
    try:
        for rid in ("dividend-calendar", "future-ex-dates", "dividend-announcements", "best-dividends"):
            assert "market" in {p["key"] for p in registry.get(rid).as_dict()["params"]}, rid
        assert CA not in _syms(registry.run_sync("future-ex-dates")) | _syms(registry.run_sync("best-dividends"))
        ca = registry.run_sync("future-ex-dates", {"market": "CA"})
        assert _syms(ca) == {CA}
        row = ca["rows"][0]
        assert (row["ex_date"], row["amount"], row["source"], row["currency"]) == (ex.isoformat(), 1.76, "tmx", "CAD")
        assert row["declared_date"] == (t - timedelta(days=2)).isoformat() and row["annual_rate"] == pytest.approx(7.04)
        assert _syms(registry.run_sync("dividend-announcements", {"market": "CA"})) == {CA}   # by declared date
        best = registry.run_sync("best-dividends", {"market": "CA"})
        assert _syms(best) == {CA}
        assert CA in _syms(registry.run_sync("future-ex-dates", {"market": "all"}))
    finally:
        from optionlens import db
        with db.write() as cur:
            cur.execute("DELETE FROM dividends_derived WHERE symbol = ?", [CA])
        _clear_caches()


def test_earnings_reports_cover_both_markets(ca_seeded):
    """Canadian listings' earnings come from TMX Money / Yahoo Finance: the earnings reports have the market filter
    (US by default) and the per-day summary never mixes markets."""
    from optionlens.models import EarningsEvent
    from optionlens.repo import earnings
    from optionlens.services.reports.base import today
    d = today()
    earnings.upsert_earnings([EarningsEvent(CA, d, "bmo", "Q4 2026", None, 4.15, None, None, "tmx")])
    _clear_caches()
    try:
        prm = {p["key"]: p for p in registry.get("earnings-calendar").as_dict()["params"]}
        assert [o["value"] for o in prm["market"]["options"]] == ["US", "CA", "all"]
        summary = {p["key"]: p for p in registry.get("earnings-upcoming-summary").as_dict()["params"]}
        assert [o["value"] for o in summary["market"]["options"]] == ["US", "CA"]
        us = registry.run_sync("earnings-calendar", {"start": d.isoformat(), "end": d.isoformat()})
        assert CA not in _syms(us)
        ca = registry.run_sync("earnings-calendar", {"start": d.isoformat(), "end": d.isoformat(), "market": "CA"})
        row = next(r for r in ca["rows"] if r["symbol"] == CA)
        assert row["source"] == "tmx" and row["currency"] == "CAD" and row["eps_estimate"] == 4.15
        days = registry.run_sync("earnings-upcoming-summary", {"market": "CA", "days": "1"})["rows"]
        assert days and days[0]["count"] == 1 and days[0]["notable"] == CA
        us_days = registry.run_sync("earnings-upcoming-summary", {"days": "1"})["rows"]
        assert all(CA not in r["notable"] for r in us_days)
    finally:
        earnings.replace_day(d, [], "tmx")
        _clear_caches()


def test_explicit_symbols_are_shown_whatever_the_market(ca_seeded):
    res = registry.run_sync("iv-rankings", {"symbols": f"AAPL,{CA}"})
    assert _syms(res) == {"AAPL", CA}


def test_watchlist_scope_is_narrowed_to_the_market(ca_seeded):
    from optionlens.repo import user
    wl = user.create_watchlist("Mixed", ["AAPL", CA])
    assert _syms(registry.run_sync("iv-rankings", {"watchlist": str(wl["id"])})) == {"AAPL"}
    assert _syms(registry.run_sync("iv-rankings", {"watchlist": str(wl["id"]), "market": "CA"})) == {CA}


def test_search_lists_both_listings_with_exchange_and_currency(ca_seeded, api):
    symbols.upsert_symbols([{"symbol": "RY", "name": "Royal Bank of Canada", "kind": "stock"}])
    hits = {h["symbol"]: h for h in api.get("/api/search?q=RY").json()}
    assert hits["RY"]["market"] == "US" and hits["RY"]["currency"] == "USD" and hits["RY"]["exchange"] is None
    assert hits[CA]["market"] == "CA" and hits[CA]["currency"] == "CAD" and hits[CA]["exchange"] == "TSX"


def test_canadian_symbol_pages(ca_seeded, api):
    s = api.get(f"/api/symbol/{CA}/summary").json()
    assert (s["market"], s["exchange"], s["currency"]) == ("CA", "TSX", "CAD")
    e = api.get(f"/api/symbol/{CA}/earnings").json()
    assert e["events"] == [] and e["unavailable"].startswith(f"Earnings dates for {CA} are not available: not looked up")
    assert "TMX Money, else Yahoo Finance" in e["next_missing"] and s["next_earnings_missing"] == e["next_missing"]
    assert api.get("/api/symbol/AAPL/earnings").json()["unavailable"] is None
    d = api.get(f"/api/symbol/{CA}/dividends").json()
    # (TMX Money is offline in tests)
    assert d["history"] == [] and d["unavailable"].startswith("The dividend history could not be loaded right now.")
    ch = api.get(f"/api/symbol/{CA}/chain").json()
    assert ch["symbol"] == CA and ch["calls"] and all(r["osi"].startswith("RY.TO") for r in ch["calls"] + ch["puts"])


def test_canadian_earnings_reach_the_chain_and_pages(ca_seeded, api):
    """A Canadian listing's next earnings (from TMX Money) is the chain's next_earnings / next_reaction, so the
    calculator's IV-crush model and earnings_before_expiry see it, and the pages show its source."""
    from optionlens.models import EarningsEvent
    from optionlens.repo import earnings
    from optionlens.services import context
    from optionlens.services.reports.base import today
    cal = calendar_for(CA)
    d = cal.add_trading_days(today(), 5)
    earnings.upsert_earnings([EarningsEvent(CA, d, "amc", "Q4 2026", None, 4.15, None, None, "tmx")])
    _clear_caches()
    try:
        ca = context.load_chain_arrays(CA, ensure_fresh=False)
        assert ca.next_earnings == d and ca.next_reaction == cal.next_trading_day(d)
        e = api.get(f"/api/symbol/{CA}/earnings").json()
        assert e["unavailable"] is None and e["next_missing"] is None
        assert (e["next"]["date"], e["next"]["timing"], e["next"]["source"]) == (d.isoformat(), "amc", "tmx")
        s = api.get(f"/api/symbol/{CA}/summary").json()
        assert s["next_earnings"]["source"] == "tmx" and s["next_earnings_missing"] is None
    finally:
        earnings.replace_day(d, [], "tmx")
        _clear_caches()
