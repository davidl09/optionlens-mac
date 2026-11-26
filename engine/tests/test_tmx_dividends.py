"""TMX Money dividend histories of Canadian listings (sources/tmx_dividends.py): parsing recorded answers
(tests/fixtures/tmx_dividends_*.json: live answers of 2026-11-26, trimmed), storing them as TMX's rows of the merged
dividends (jobs.ondemand kind "dividends"), the daily job, and what the Dividends tab says."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from optionlens import db, net
from optionlens.jobs import calendars, ondemand, scheduler
from optionlens.models import Bar, Dividend
from optionlens.net import FetchError
from optionlens.repo import bars as bars_repo
from optionlens.repo import dividends as div_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.services import context as ctx
from optionlens.services import symbol as symbol_svc
from optionlens.sources import tmx, tmx_dividends

FIX = Path(__file__).parent / "fixtures"
_REAL_TMX_QUERY = tmx.query
BY_TICKER = {"CNQ": "tmx_dividends_CNQ.json", "XIU": "tmx_dividends_XIU.json", "MG": "tmx_dividends_MG.json",
             "SHOP": "tmx_dividends_SHOP.json"}


def _json(name: str):
    return json.loads((FIX / name).read_text())


def _history(ticker: str, symbol: str | None = None):
    page = tmx_dividends.parse_page(_json(BY_TICKER[ticker]))
    return tmx_dividends.dividends(symbol or f"{ticker}.TO", page.rows, page.splits)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    ctx.invalidate()
    yield
    db.close()
    ctx.invalidate()


@pytest.fixture
def tmx_answers(monkeypatch):
    """TMX answering each ticker from its fixture (unknown tickers: no dividends); returns the tickers asked."""
    asked = []

    async def fake(op, text, variables):
        asked.append(variables["symbol"])
        return _json(BY_TICKER.get(variables["symbol"], "tmx_dividends_unknown.json"))

    monkeypatch.setattr(tmx, "query", fake)
    return asked


# ----------------------------------------------------------------------------- parsing
def test_history_is_in_todays_shares_with_every_date():
    divs, other = _history("CNQ")
    assert other == {} and [d.ex_date for d in divs] == sorted(d.ex_date for d in divs)
    by = {d.ex_date: d for d in divs}
    nxt = by[date(2026, 12, 15)]                                       # declared, not yet paid
    assert nxt == Dividend("CNQ.TO", date(2026, 12, 15), 0.625, date(2027, 1, 5), date(2026, 12, 15),
                           date(2026, 11, 4), "cash", source="tmx")
    # declared 1.05 before the 2:1 split of 2024-06-11: 0.525 in today's shares, as the first payment after it
    assert by[date(2024, 3, 14)].amount == 0.525 and by[date(2024, 6, 17)].amount == 0.525
    assert by[date(2022, 8, 22)].amount == 0.75                        # the special 1.50 of 2022, halved too
    assert all(d.source == "tmx" and d.kind == "cash" and d.amount > 0 for d in divs)


def test_a_fund_without_cash_amounts_and_an_older_split():
    raw = _json("tmx_dividends_XIU.json")["data"]["dividendHistory"]["dividends"]
    divs, other = _history("XIU")
    assert sum(1 for r in raw if not r["amount"]) >= 3 and other == {}
    assert len(divs) == len({r["exDate"] for r in raw if r["amount"]})   # amounts of 0 are not dividends
    by = {d.ex_date: d.amount for d in divs}
    assert date(2026, 12, 30) not in by                                # declared without an amount yet
    assert by[date(2008, 6, 25)] == pytest.approx(0.49049 * 0.25, abs=1e-6)  # before the 4:1 split of 2008-08-06
    assert by[date(2008, 9, 25)] == 0.12976                            # after it: as declared


def test_dividends_in_another_currency_are_left_out_and_counted():
    divs, other = _history("MG")
    assert divs == [] and other == {"USD": 4}


def test_no_dividends_and_unknown_tickers():
    assert _history("SHOP") == ([], {})
    page = tmx_dividends.parse_page(_json("tmx_dividends_unknown.json"))   # "splits" errors for a ticker without any
    assert page.rows == [] and page.splits == [] and not page.more
    with pytest.raises(tmx.Unexpected):
        tmx_dividends.parse_page({"errors": [{"message": 'Cannot query field "exDate"'}]})
    with pytest.raises(tmx.Unexpected):
        tmx_dividends.parse_page({"data": {"dividendHistory": None, "splits": []}})


def test_two_payments_with_one_ex_date_are_one_row():
    rows = [{"exDate": "2026-03-02", "amount": 0.5, "currency": "CAD", "payableDate": "2026-03-20"},
            {"exDate": "2026-03-02", "amount": 1.0, "currency": "CAD"}, {"exDate": None, "amount": 1.0},
            {"exDate": "2026-06-01", "amount": "n/a", "currency": "CAD"},
            {"exDate": "1990-06-01", "amount": 0.2, "currency": None}]         # no currency: not shown, not counted
    divs, other = tmx_dividends.dividends("ABC.TO", rows, [])
    assert [(d.ex_date, d.amount) for d in divs] == [(date(2026, 3, 2), 1.5)] and other == {}


async def test_one_request_asks_dividends_and_splits_as_optionlens(monkeypatch):
    sent = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        sent.append((request.method, request.headers["user-agent"], body["operationName"], body["variables"]))
        assert "getDividendsForSymbol" in body["query"] and "getSplitsForSymbol" in body["query"]
        return httpx.Response(200, json=_json("tmx_dividends_CNQ.json"))

    monkeypatch.setattr(tmx, "query", _REAL_TMX_QUERY)   # the real one (the suite guards it) over a mock transport
    monkeypatch.setattr(net, "POLITE_RPS", 1000.0)
    n = net._net()
    await n.client.aclose()
    n.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        divs, other = await tmx_dividends.fetch_history("CNQ.TO")
    finally:
        await net.aclose()
    assert len(divs) == 20 and other == {}
    assert sent == [("POST", net.HONEST_USER_AGENT, "getDividendsForSymbol", {"symbol": "CNQ", "page": 1, "batch": 500})]
    with pytest.raises(ValueError):
        await tmx_dividends.fetch_history("CNQ")          # a US listing is never asked of TMX


# ----------------------------------------------------------------------------- storing
CA_SYMBOLS = [{"symbol": "CNQ.TO", "name": "CANADIAN NATURAL", "kind": "stock", "in_universe": True},
              {"symbol": "XIU.TO", "name": "ISHARES S&P/TSX 60", "kind": "etf", "in_universe": True},
              {"symbol": "MG.TO", "name": "MAGNA", "kind": "stock"}, {"symbol": "SHOP.TO", "name": "SHOPIFY", "kind": "stock"},
              {"symbol": "AAPL", "name": "Apple", "kind": "stock", "in_universe": True}]


@pytest.fixture
def ca_db(tmpdb):
    symbols_repo.upsert_symbols(CA_SYMBOLS)
    symbols_repo.set_universe(["AAPL"])
    from optionlens.analytics import markets
    symbols_repo.set_universe(["CNQ.TO", "XIU.TO"], markets.CA)
    # a stored price history: opening a symbol fetches no prices
    bars_repo.upsert_bars([Bar(s["symbol"], date(2026, 8 + i // 28, 1 + i % 28), 50, 51, 49, 50, 1000)
                           for s in CA_SYMBOLS for i in range(84)])
    return tmpdb


async def test_the_history_is_stored_once_per_cache_time_and_merged(ca_db, tmx_answers):
    now = datetime.now(timezone.utc)
    div_repo.upsert_derived([Dividend("CNQ.TO", date(2026, 12, 15), 0.62, None, None, None, "cash", "questrade"),
                             Dividend("CNQ.TO", date(2019, 1, 2), 9.99, None, None, None, "cash", "tmx")], now)
    got = await ondemand.fetch_cached("dividends", "CNQ.TO")
    assert got == {"dividends": 20, "other_currency": {}}
    assert await ondemand.fetch_cached("dividends", "CNQ.TO") == got and tmx_answers == ["CNQ"]    # cached
    divs = div_repo.for_symbol("CNQ.TO")
    assert len(divs) == 20 and {d.source for d in divs} == {"tmx"}     # what TMX no longer lists is gone
    nxt = divs[-1]                                                     # TMX's row over Questrade's current dividend
    assert (nxt.ex_date, nxt.amount, nxt.pay_date, nxt.declared_date) == (date(2026, 12, 15), 0.625, date(2027, 1, 5),
                                                                          date(2026, 11, 4))
    assert not nxt.discrepancy and div_repo.next_ex(date(2026, 11, 26))["CNQ.TO"].amount == 0.625
    assert await ondemand.fetch_cached("dividends", "AAPL") is None and tmx_answers == ["CNQ"]     # US: jobs only


async def test_an_answer_without_dividends_keeps_the_stored_rows(ca_db, monkeypatch):
    now = datetime.now(timezone.utc)
    div_repo.upsert_derived([Dividend("CNQ.TO", date(2026, 9, 11), 0.625, None, None, None, "cash", "tmx")], now)

    async def empty(op, text, variables):
        return _json("tmx_dividends_SHOP.json")

    monkeypatch.setattr(tmx, "query", empty)
    assert await ondemand.fetch_cached("dividends", "CNQ.TO") == {"dividends": 0, "other_currency": {}}
    assert [d.amount for d in div_repo.for_symbol("CNQ.TO")] == [0.625]


async def test_the_daily_job_asks_every_canadian_listing_once(ca_db, tmx_answers):
    ok, detail = await scheduler.run_job("dividends_ca")
    assert ok and detail == "2 of 4 listings with dividends; 1 pay in another currency (not stored)"
    assert tmx_answers == ["CNQ", "XIU", "MG", "SHOP"]                 # the universe first; no US listing
    assert len(div_repo.for_symbol("XIU.TO")) == 11 and div_repo.for_symbol("MG.TO") == []
    ok, detail = await scheduler.run_job("dividends_ca")               # the same day: nothing is asked again
    assert ok and len(tmx_answers) == 4


async def test_the_daily_job_stops_at_a_failure_and_keeps_what_it_stored(ca_db, monkeypatch):
    asked = []

    async def flaky(op, text, variables):
        asked.append(variables["symbol"])
        if len(asked) == 2:
            raise FetchError(tmx.URL, 503)
        return _json(BY_TICKER[variables["symbol"]])

    monkeypatch.setattr(tmx, "query", flaky)
    ok, detail = await scheduler.run_job("dividends_ca")
    assert ok and detail.startswith("1 of 1 listings with dividends; TMX Money unavailable after 1/4: 503")
    assert asked == ["CNQ", "XIU"] and len(div_repo.for_symbol("CNQ.TO")) == 20
    asked.clear()

    async def down(op, text, variables):
        raise FetchError(tmx.URL, 503)

    monkeypatch.setattr(tmx, "query", down)
    with db.write() as cur:
        cur.execute("DELETE FROM http_cache")
    ok, detail = await scheduler.run_job("dividends_ca")
    assert not ok and "503" in detail                                  # nothing could be fetched: the run fails


async def test_without_canadian_listings_nothing_is_asked(tmpdb, tmx_answers):
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "name": "Apple", "kind": "stock"}])
    assert await calendars.dividends_ca() == calendars.NO_CA and tmx_answers == []


# ----------------------------------------------------------------------------- the Dividends tab
def test_the_dividends_tab_of_a_canadian_listing(ca_db, tmx_answers, monkeypatch):
    monkeypatch.setattr(ctx, "today", lambda: date(2026, 11, 26))
    d = symbol_svc.dividends("CNQ.TO")
    assert d["unavailable"] is None and len(d["history"]) == 20 and tmx_answers == ["CNQ"]
    assert d["history"][0]["ex_date"] == date(2026, 12, 15) and d["history"][0]["source"] == "tmx"
    assert d["annual_rate"] == pytest.approx(2.5) and d["frequency"] == 4
    assert d["next"]["ex_date"] == date(2026, 12, 15)
    special = next(h for h in d["history"] if h["ex_date"] == date(2022, 8, 22))
    assert special["amount"] == 0.75 and special["one_off"] is False   # (0.75 beside 0.375 regulars: under 2.5×)
    symbol_svc.dividends("CNQ.TO")
    assert tmx_answers == ["CNQ"]                                      # a second view does not ask again


def test_the_dividends_tab_says_why_a_history_is_missing(ca_db, tmx_answers, monkeypatch):
    usd = symbol_svc.dividends("MG.TO")
    assert usd["history"] == [] and usd["unavailable"] == (
        "This listing pays its dividends in USD, not in CAD: its history is not shown (amounts are not converted). "
        "Shown: what the other sources have (IBKR's dividend-adjusted prices, Questrade's current dividend).")
    none = symbol_svc.dividends("SHOP.TO")
    assert none["history"] == [] and none["unavailable"] is None       # pays none: the tab's own empty sentence

    async def down(op, text, variables):
        raise FetchError(tmx.URL, 503)

    monkeypatch.setattr(tmx, "query", down)
    failed = symbol_svc.dividends("XIU.TO")
    assert failed["unavailable"] == ("The dividend history could not be loaded right now. Shown: what the other "
                                     "sources have (IBKR's dividend-adjusted prices, Questrade's current dividend).")
    assert symbol_svc.dividends("AAPL")["unavailable"] is None         # US listings: unchanged, TMX never asked
    assert tmx_answers == ["MG", "SHOP"]
