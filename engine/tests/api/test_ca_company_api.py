"""Company tabs of a Canadian listing (insiders, news, profile) come from TMX Money through the on-demand cache; US
listings stay on Nasdaq; a TMX failure with nothing stored is a plain sentence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from datetime import date

from optionlens import db
from optionlens.net import FetchError
from optionlens.repo import bars, symbols
from optionlens.services import context
from optionlens.sources import tmx

from .seed_reports import _bars

FIX = Path(__file__).resolve().parents[1] / "fixtures"
CA, S = "RY.TO", "/api/symbol/RY.TO"
ANSWERS = {"getQuoteBySymbol": "tmx_profile_RY.json", "getNewsForSymbol": "tmx_news_RY.json",
           "getCompanyInsidersActivities": "tmx_insider_summary_RY.json",
           "getInsiderTransactions": "tmx_insider_transactions_RY.json"}


def get(client, url, status=200):
    r = client.get(url)
    assert r.status_code == status, r.text
    return r.json()


@pytest.fixture
def canadian(client):
    """RY.TO and the fund XIU.TO as known symbols, with no company data stored before or after."""
    def forget():
        with db.write() as cur:
            cur.execute("DELETE FROM http_cache WHERE key LIKE '%.TO'")

    symbols.upsert_symbols([{"symbol": CA, "name": "ROYAL BANK OF CANADA", "kind": "stock"},
                            {"symbol": "XIU.TO", "name": "ISHARES S&P/TSX 60", "kind": "etf"}])
    for sym in (CA, "XIU.TO"):                              # a stored history: opening the symbol fetches no prices
        bars.upsert_bars(_bars(sym, 100.0, date(2026, 11, 19), n=60))
    forget()
    context.invalidate()
    yield client
    forget()
    with db.write() as cur:
        cur.execute("DELETE FROM daily_bars WHERE symbol LIKE '%.TO'")
        cur.execute("DELETE FROM symbols WHERE symbol LIKE '%.TO'")
    context.invalidate()


@pytest.fixture
def tmx_answers(monkeypatch):
    """TMX answering from the recorded fixtures; returns the list of (operation, ticker) asked."""
    asked = []

    async def fake(op, text, variables):
        asked.append((op, variables["symbol"]))
        return json.loads((FIX / ANSWERS[op]).read_text())

    monkeypatch.setattr(tmx, "query", fake)
    return asked


def test_profile_news_and_insiders_of_a_canadian_listing(canadian, tmx_answers):
    p = get(canadian, f"{S}/profile")
    assert p["name"] == "Royal Bank of Canada" and p["exchange"] == "TSX" and p["website"] == "https://www.rbc.com"
    assert (p["sector"], p["industry"]) == ("Finance", "Banking") and p["description"].startswith("Royal Bank of Canada")
    news = get(canadian, f"{S}/news")
    assert len(news) == 5 and news[0]["source"] == "Canada Newswire via QuoteMedia"
    assert news[0]["published"] == "2026-11-25T06:11:00-05:00" and news[0]["summary"] is None
    assert all(n["url"].startswith("https://money.tmx.com/quote/RY/news/") and n["title"] for n in news)
    ins = get(canadian, f"{S}/insiders")
    assert ins["source"] == "tmx" and ins["summary"][0] == {"label": "Buy trades", "m3": "10", "m12": "45"}
    assert ins["summary"][5] == {"label": "Net activity (shares)", "m3": "-1,586", "m12": "5,481"}
    sale = next(t for t in ins["trades"] if t["transaction"] == "Disposition in the public market · Common Shares")
    assert sale["shares"] == -9604 and sale["value"] == pytest.approx(-9604 * 275.2011) and sale["date"] == "2026-10-01"
    assert tmx_answers == [("getQuoteBySymbol", "RY"), ("getNewsForSymbol", "RY"),
                           ("getCompanyInsidersActivities", "RY"), ("getInsiderTransactions", "RY")]


def test_a_second_view_is_served_from_the_cache(canadian, tmx_answers):
    for _ in range(2):
        for tab in ("profile", "news", "insiders"):
            get(canadian, f"{S}/{tab}")
    assert len(tmx_answers) == 4                              # one fetch per tab (insiders: two requests)


def test_stored_data_outlives_a_tmx_failure(canadian, tmx_answers, monkeypatch):
    first = {tab: get(canadian, f"{S}/{tab}") for tab in ("profile", "news", "insiders")}
    with db.write() as cur:                                   # every entry is now older than its TTL
        cur.execute("UPDATE http_cache SET fetched_at = fetched_at - INTERVAL 30 DAY WHERE key LIKE '%RY.TO'")

    async def down(op, text, variables):
        raise FetchError(tmx.URL, 503)

    monkeypatch.setattr(tmx, "query", down)
    assert {tab: get(canadian, f"{S}/{tab}") for tab in first} == first


def test_a_tmx_failure_with_nothing_stored_is_a_plain_sentence(canadian):
    # (the suite's guard makes every TMX request fail)
    for tab, what in (("profile", "The company profile"), ("news", "News"), ("insiders", "Insider data")):
        detail = get(canadian, f"{S}/{tab}", 502)["detail"]
        assert detail == f"{what} could not be loaded right now. Try again in a few minutes."


def test_a_canadian_fund_has_a_profile_and_no_insider_reports(canadian, monkeypatch):
    asked = []

    async def fake(op, text, variables):
        asked.append(op)
        return json.loads((FIX / "tmx_profile_XIU.json").read_text())

    monkeypatch.setattr(tmx, "query", fake)
    p = get(canadian, "/api/symbol/XIU.TO/profile")
    assert p["name"] == "iShares S&P/TSX 60 Index ETF" and p["website"] == "https://www.ishares.com"
    assert p["sector"] is None and p["description"].startswith("The investment objective")
    assert get(canadian, "/api/symbol/XIU.TO/insiders") == {"symbol": "XIU.TO", "source": "tmx", "summary": [],
                                                           "trades": []}
    assert asked == ["getQuoteBySymbol"]                      # funds file no insider reports: not asked


def test_a_ticker_tmx_does_not_know_has_empty_tabs(canadian, monkeypatch):
    unknown = {"getQuoteBySymbol": "tmx_profile_unknown.json",
               "getCompanyInsidersActivities": "tmx_insider_summary_unknown.json"}

    async def fake(op, text, variables):
        if op in unknown:
            return json.loads((FIX / unknown[op]).read_text())
        return {"data": {"news": []}} if op == "getNewsForSymbol" else {"data": {"getInsiderTransactions": []}}

    monkeypatch.setattr(tmx, "query", fake)
    p = get(canadian, f"{S}/profile")
    assert p["name"] == "ROYAL BANK OF CANADA" and p["description"] is None and p["website"] is None
    assert get(canadian, f"{S}/news") == []
    assert get(canadian, f"{S}/insiders") == {"symbol": CA, "source": "tmx", "summary": [], "trades": []}


def test_us_listings_stay_on_nasdaq(client, tmx_answers):
    ins = get(client, "/api/symbol/AAPL/insiders")
    assert ins["source"] == "nasdaq" and ins["summary"][1]["m12"] == "45"
    assert get(client, "/api/symbol/AAPL/news")[0]["source"] == "Barchart"
    p = get(client, "/api/symbol/AAPL/profile")
    assert p["website"] == "https://www.apple.com" and p["exchange"] == "NASDAQ-GS"
    assert tmx_answers == []
