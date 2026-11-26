"""TMX Money company data of Canadian listings (sources/tmx_company.py): parsers on recorded answers
(tests/fixtures/tmx_profile_*.json, tmx_news_RY.json, tmx_insider_*.json: live answers of 2026-11-26, trimmed), the
fetchers' requests, and which source jobs.ondemand asks per market."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from optionlens import net
from optionlens.jobs import ondemand
from optionlens.net import FetchError
from optionlens.sources import tmx, tmx_company

FIX = Path(__file__).parent / "fixtures"
_REAL_TMX_QUERY = tmx.query


def _json(name: str):
    return json.loads((FIX / name).read_text())


def test_profile_of_a_company_a_fund_and_an_unknown_ticker():
    p = tmx_company.parse_profile(_json("tmx_profile_RY.json"))
    assert p["CompanyName"] == "Royal Bank of Canada" and p["Exchange"] == "TSX"
    assert (p["Sector"], p["Industry"], p["CompanyUrl"]) == ("Finance", "Banking", "https://www.rbc.com")
    assert p["CompanyDescription"].startswith("Royal Bank of Canada is a global financial institution")
    etf = tmx_company.parse_profile(_json("tmx_profile_XIU.json"))
    assert etf["CompanyName"] == "iShares S&P/TSX 60 Index ETF" and etf["Sector"] is None and etf["Industry"] is None
    assert etf["CompanyUrl"] == "https://www.ishares.com"            # TMX gives a bare host
    assert etf["CompanyDescription"].startswith("The investment objective of the Fund")
    assert tmx_company.parse_profile(_json("tmx_profile_unknown.json")) is None
    with pytest.raises(tmx.Unexpected):
        tmx_company.parse_profile({"errors": [{"message": "Internal server error"}], "data": None})


@pytest.mark.parametrize("website,url", [
    ("https://www.rbc.com", "https://www.rbc.com"), ("www.ishares.com", "https://www.ishares.com"),
    ("http://example.com", None), ("javascript:alert(1)", None), ("", None), (None, None), ("two words", None),
])
def test_only_https_websites_are_linked(website, url):
    payload = {"data": {"getQuoteBySymbol": {"name": "X", "website": website}}}
    assert tmx_company.parse_profile(payload)["CompanyUrl"] == url


def test_news_lists_each_release_once_with_an_https_story_link():
    raw = _json("tmx_news_RY.json")["data"]["news"]
    items = tmx_company.parse_news(_json("tmx_news_RY.json"), "RY")
    assert len(raw) == 7 and len(items) == 5                          # two releases came from two wires each
    first = items[0]
    assert first["title"].startswith("Is FOMU keeping Canadians from investing their cash?")
    assert first["publisher"] == "Canada Newswire via QuoteMedia" and first["created"] == "2026-11-25T06:11:00-05:00"
    assert first["url"] == "https://money.tmx.com/quote/RY/news/8848269543440233"
    assert all(i["url"].startswith("https://money.tmx.com/quote/RY/news/") for i in items)
    assert [i["created"] for i in items] == sorted((i["created"] for i in items), reverse=True)
    assert tmx_company.parse_news({"data": {"news": []}}, "ZZZZQ") == []
    odd = {"data": {"news": [{"headline": "A", "datetime": "soon", "source": None, "newsid": 12},
                             {"headline": "", "newsid": 13}, {"headline": "B", "newsid": "x/../y"}]}}
    assert tmx_company.parse_news(odd, "RCI.B") == [{"id": "12", "title": "A", "publisher": None, "created": None,
                                                     "url": "https://money.tmx.com/quote/RCI.B/news/12"}]


def test_insiders_totals_and_sedi_reports():
    raw = _json("tmx_insider_transactions_RY.json")["data"]["getInsiderTransactions"]
    d = tmx_company.parse_insiders(_json("tmx_insider_summary_RY.json"), _json("tmx_insider_transactions_RY.json"))
    assert d["summary"]["trades"] == [{"label": "Buy trades", "months3": 10.0, "months12": 45.0},
                                      {"label": "Sell trades", "months3": 14.0, "months12": 40.0},
                                      {"label": "Total trades", "months3": 24.0, "months12": 85.0}]
    assert d["summary"]["shares"][2] == {"label": "Net activity (shares)", "months3": -1586.0, "months12": 5481.0}
    issuer = [r for r in raw if r["relationship"] == "Issuer"]
    assert issuer and len(d["trades"]) == len(raw) - len(issuer)       # the company's own buybacks are left out
    assert all(t["insider"] != "Royal Bank of Canada" for t in d["trades"])
    assert [t["date"] for t in d["trades"]] == sorted((t["date"] for t in d["trades"]), reverse=True)
    sale = next(t for t in d["trades"] if t["transaction"].startswith("Disposition in the public market"))
    assert sale == {"insider": "Douvas, Maria Elena", "relation": "Senior Officer of Issuer", "date": date(2026, 10, 1),
                    "transaction": "Disposition in the public market · Common Shares", "shares": -9604,
                    "price": 275.2011, "shares_held": 0}
    usd = d["trades"][0]
    assert usd["transaction"] == "Stock dividend · Rights Performance Deferred Share Units · price in USD"
    assert usd["shares"] == 358 and usd["price"] == 189.19 and usd["shares_held"] == 54217
    opening = next(t for t in d["trades"] if t["transaction"].startswith("Opening Balance"))
    assert opening["price"] is None and opening["shares"] == 0 and opening["shares_held"] is None


def test_insiders_of_a_ticker_tmx_does_not_know_are_empty():
    d = tmx_company.parse_insiders(_json("tmx_insider_summary_unknown.json"), {"data": {"getInsiderTransactions": []}})
    assert d == {"summary": {"trades": [], "shares": []}, "trades": []}


def test_insiders_keep_the_latest_reports(monkeypatch):
    monkeypatch.setattr(tmx_company, "INSIDER_ROWS", 3)
    d = tmx_company.parse_insiders(_json("tmx_insider_summary_RY.json"), _json("tmx_insider_transactions_RY.json"))
    assert [t["date"] for t in d["trades"]] == [date(2026, 11, 24)] * 3


async def test_fetchers_ask_tmx_by_its_ticker_as_optionlens(monkeypatch):
    sent = []
    answers = {"getQuoteBySymbol": "tmx_profile_RY.json", "getNewsForSymbol": "tmx_news_RY.json",
               "getCompanyInsidersActivities": "tmx_insider_summary_RY.json",
               "getInsiderTransactions": "tmx_insider_transactions_RY.json"}

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        sent.append((request.method, str(request.url), request.headers["user-agent"], body["operationName"],
                     body["variables"]))
        return httpx.Response(200, json=_json(answers[body["operationName"]]))

    monkeypatch.setattr(tmx, "query", _REAL_TMX_QUERY)   # the real one (the suite guards it) over a mock transport
    monkeypatch.setattr(net, "POLITE_RPS", 1000.0)       # (the real gate asks once a second)
    n = net._net()
    await n.client.aclose()
    n.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        prof = await tmx_company.fetch_profile("RCI.B.TO")
        news = await tmx_company.fetch_news("RCI.B.TO")
        ins = await tmx_company.fetch_insiders("RCI.B.TO")
    finally:
        await net.aclose()
    assert prof["CompanyName"] and len(news) == 5 and ins["trades"]
    assert news[0]["url"].startswith("https://money.tmx.com/quote/RCI.B/news/")
    assert {(m, url, ua) for m, url, ua, _, _ in sent} == {("POST", tmx.URL, net.HONEST_USER_AGENT)}
    assert [(op, v) for *_, op, v in sent] == [
        ("getQuoteBySymbol", {"symbol": "RCI.B", "locale": "en"}),
        ("getNewsForSymbol", {"symbol": "RCI.B", "page": 1, "limit": 20, "locale": "en"}),
        ("getCompanyInsidersActivities", {"symbol": "RCI.B"}),
        ("getInsiderTransactions", {"symbol": "RCI.B", "monthDuration": 12})]


async def test_a_changed_answer_is_a_fetch_error(monkeypatch):
    async def changed(op, text, variables):
        return {"errors": [{"message": 'Cannot query field "longDescription"'}]}

    monkeypatch.setattr(tmx, "query", changed)
    for fetch in (tmx_company.fetch_profile, tmx_company.fetch_news, tmx_company.fetch_insiders):
        with pytest.raises(FetchError, match="TMX"):
            await fetch("RY.TO")
    with pytest.raises(ValueError):
        await tmx_company.fetch_profile("RY")            # a US listing is never asked of TMX


def test_each_market_has_its_company_source():
    for kind in ("profile", "news", "insiders"):
        assert ondemand.source_of(kind, "T") == "nasdaq" and ondemand.source_of(kind, "T.TO") == "tmx"
        assert ondemand.source_of(kind, "XYZ.VN") == "tmx"
    for kind in ("summary", "financials", "info", "earnings_surprise", "extended"):
        assert ondemand.source_of(kind, "T") == "nasdaq" and ondemand.source_of(kind, "T.TO") is None
