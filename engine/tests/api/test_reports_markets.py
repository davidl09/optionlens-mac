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
    for rid in ("earnings-calendar", "dividend-calendar", "splits-calendar", "premarket-movers"):
        assert "market" not in {p["key"] for p in registry.get(rid).as_dict()["params"]}, rid
    assert CA not in _syms(registry.run_sync("best-dividends"))


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
    assert e["events"] == [] and "not available" in e["unavailable"]
    assert api.get("/api/symbol/AAPL/earnings").json()["unavailable"] is None
    d = api.get(f"/api/symbol/{CA}/dividends").json()
    assert d["history"] == [] and "current dividend Questrade reports" in d["unavailable"]
    ch = api.get(f"/api/symbol/{CA}/chain").json()
    assert ch["symbol"] == CA and ch["calls"] and all(r["osi"].startswith("RY.TO") for r in ch["calls"] + ch["puts"])
