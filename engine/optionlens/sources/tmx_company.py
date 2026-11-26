"""TMX Money company data of Canadian listings: profile, news and insider reports, in the normalized shapes
sources/nasdaq.py gives for US listings (services/symbol_company.py reads either). UNOFFICIAL, like sources/tmx.py
(the client and its politeness rules): the site's own GraphQL operations, read 2026-11-26;
tests/fixtures/tmx_profile_*.json, tmx_news_*.json, tmx_insider_*.json are recorded answers (trimmed).

Operations:
- getQuoteBySymbol(symbol, locale): the quote page's company block: name, exShortName (TSX / TSXV), sector, industry,
  longDescription, website ("https://www.rbc.com", or a bare host for some funds). ETFs answer too (empty sector /
  industry). An unknown ticker: an error with code "404" and null data.
- getNewsForSymbol(symbol, page, limit, locale): press releases, newest first: headline, datetime (ISO with offset),
  source ("Canada Newswire via QuoteMedia"), newsid. The same release often arrives from two wires. The site shows a
  story at money.tmx.com/quote/<ticker>/news/<newsid>. An unknown ticker: [].
- getCompanyInsidersActivities(symbol): TMX's totals of insider trading over 3 / 6 / 12 months (activitySummary:
  buyTrades, sellTrades, totalTrades, buyShares, soldShares, netActivity). An unknown ticker: an error
  ("Cannot read properties of undefined") and null data.
- getInsiderTransactions(symbol, monthDuration): the SEDI insider reports of the last months, newest first: filer,
  relationship ("Senior Officer of Issuer", "Director of Issuer", "Issuer" for the company's own buybacks), date,
  type ("Exercise of options", ...), transactionTypeCode (1 = acquisition or disposition in the public market),
  securitydesignation ("Common Shares", "Options", "Rights ..."), amount (signed: negative = disposed), pricefrom,
  pricefromcurrency, amountowned (balance after). A ticker without reports: [].

Parsers are pure; fetchers do I/O via tmx.query (FetchError when TMX fails or answers unexpectedly).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import quote

from .. import net
from . import tmx

NEWS_LIMIT = 20              # headlines asked for (one page)
INSIDER_MONTHS = 12          # window of SEDI reports asked for
INSIDER_ROWS = 200           # latest reports kept (a bank files hundreds a year)
ISSUER = "Issuer"            # relationship of the company's own reports (buybacks): not an insider's trade
PUBLIC_MARKET = 1            # transactionTypeCode: acquisition or disposition in the public market
LISTING_CURRENCY = "CAD"
STORY_URL = "https://money.tmx.com/quote/{ticker}/news/{newsid}"

PROFILE_QUERY = """query getQuoteBySymbol($symbol: String, $locale: String) {
  getQuoteBySymbol(symbol: $symbol, locale: $locale) {
    symbol name exchangeName exShortName exchangeCode sector industry longDescription fulldescription website
    employees datatype
  }
}"""
NEWS_QUERY = """query getNewsForSymbol($symbol: String!, $page: Int!, $limit: Int!, $locale: String!) {
  news: getNewsForSymbol(symbol: $symbol, page: $page, limit: $limit, locale: $locale) {
    headline datetime source newsid
  }
}"""
INSIDER_SUMMARY_QUERY = """query getCompanyInsidersActivities($symbol: String) {
  getCompanyInsidersActivities(symbol: $symbol) {
    activitySummary { periodkey buyShares soldShares netActivity totalShares buyTrades sellTrades totalTrades }
  }
}"""
INSIDER_TRANSACTIONS_QUERY = """query getInsiderTransactions($symbol: String!, $monthDuration: Int) {
  getInsiderTransactions(symbol: $symbol, monthDuration: $monthDuration)
}"""

_SUMMARY_ROWS = {"trades": (("Buy trades", "buyTrades"), ("Sell trades", "sellTrades"), ("Total trades", "totalTrades")),
                 "shares": (("Shares bought", "buyShares"), ("Shares sold", "soldShares"),
                            ("Net activity (shares)", "netActivity"))}


def _text(v: Any) -> str | None:
    s = v.strip() if isinstance(v, str) else ""
    return s or None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _whole(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


_BARE_HOST = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(/\S*)?")
_HTTPS_URL = re.compile(r"https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+([/?#]\S*)?")


def _https(v: Any) -> str | None:
    """An https URL, or None: a bare host ("www.ishares.com") gets the scheme, anything else is not linked."""
    s = _text(v) or ""
    if _BARE_HOST.fullmatch(s):
        s = f"https://{s}"
    return s if _HTTPS_URL.fullmatch(s) else None


def _not_found(payload: Any) -> bool:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return bool(errors) and all(isinstance(e, dict) and str(e.get("code")) == "404" for e in errors)


# ----------------------------------------------------------------------------- parsers
def parse_profile(payload: Any) -> dict | None:
    """getQuoteBySymbol -> {CompanyName, CompanyDescription, CompanyUrl, Sector, Industry, Exchange} (nasdaq's
    profile keys; absent values None), or None for a ticker TMX does not know."""
    if _not_found(payload):
        return None
    d = tmx.answer(payload, "getQuoteBySymbol")
    if d is None:
        return None
    if not isinstance(d, dict):
        raise tmx.Unexpected("getQuoteBySymbol: not an object")
    return {"CompanyName": _text(d.get("name")),
            "CompanyDescription": _text(d.get("longDescription")) or _text(d.get("fulldescription")),
            "CompanyUrl": _https(d.get("website")), "Sector": _text(d.get("sector")),
            "Industry": _text(d.get("industry")),
            "Exchange": _text(d.get("exShortName")) or _text(d.get("exchangeName"))}


def parse_news(payload: Any, ticker: str) -> list[dict]:
    """getNewsForSymbol -> [{id, title, url (the story on money.tmx.com), publisher, created (ISO datetime | None)}],
    newest first; a release repeated by a second wire (same headline and time) is listed once."""
    rows = tmx.answer(payload, "news")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise tmx.Unexpected("getNewsForSymbol: not a list")
    out, seen = [], set()
    for r in rows:
        title = _text(r.get("headline")) if isinstance(r, dict) else None
        newsid = str(r.get("newsid") or "").strip() if isinstance(r, dict) else ""
        if title is None or not newsid.isdigit():
            continue
        created = _text(r.get("datetime"))
        try:
            created = datetime.fromisoformat(created).isoformat() if created else None
        except ValueError:
            created = None
        if (title, created) in seen:
            continue
        seen.add((title, created))
        out.append({"id": newsid, "title": title, "publisher": _text(r.get("source")), "created": created,
                    "url": STORY_URL.format(ticker=quote(ticker, safe="."), newsid=newsid)})
    return out


def _transaction(r: dict) -> str | None:
    """What a SEDI report says happened: its type (a public-market trade named by its direction), the security it
    is about, and the price's currency when it is not the listing's."""
    kind, amount = _text(r.get("type")), _num(r.get("amount"))
    if r.get("transactionTypeCode") == PUBLIC_MARKET and amount:
        kind = "Acquisition in the public market" if amount > 0 else "Disposition in the public market"
    parts = [p for p in (kind, _text(r.get("securitydesignation"))) if p]
    currency = _text(r.get("pricefromcurrency"))
    if currency and currency != LISTING_CURRENCY and _num(r.get("pricefrom")) is not None:
        parts.append(f"price in {currency}")
    return " · ".join(parts) or None


def _day(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def parse_insiders(summary_payload: Any, transactions_payload: Any) -> dict:
    """getCompanyInsidersActivities + getInsiderTransactions -> nasdaq's insiders shape: {summary: {trades, shares:
    [{label, months3, months12}]}, trades: [{insider, relation, date, transaction, shares (signed: negative =
    disposed), price, shares_held}]}: the insiders' own reports (not the issuer's), newest first, at most
    INSIDER_ROWS."""
    activity = tmx.answer(summary_payload, "getCompanyInsidersActivities") or {}     # [] : TMX has no such company
    periods = {p.get("periodkey"): p for p in (activity.get("activitySummary") or []) if isinstance(p, dict)}
    m3, m12 = periods.get("threeMonths"), periods.get("twelveMonths")
    summary = {part: [{"label": label, "months3": _num((m3 or {}).get(key)), "months12": _num((m12 or {}).get(key))}
                      for label, key in rows] if m3 or m12 else []
               for part, rows in _SUMMARY_ROWS.items()}
    rows = tmx.answer(transactions_payload, "getInsiderTransactions")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise tmx.Unexpected("getInsiderTransactions: not a list")
    trades = [{"insider": _text(r.get("filer")), "relation": _text(r.get("relationship")), "date": _day(r.get("date")),
               "transaction": _transaction(r), "shares": _whole(r.get("amount")), "price": _num(r.get("pricefrom")),
               "shares_held": _whole(r.get("amountowned"))}
              for r in rows if isinstance(r, dict) and r.get("relationship") != ISSUER]
    trades.sort(key=lambda t: t["date"] or date.min, reverse=True)       # stable: TMX's order within a day
    return {"summary": summary, "trades": trades[:INSIDER_ROWS]}


# ----------------------------------------------------------------------------- fetchers
async def _ask(op: str, text: str, variables: dict, parse: Callable[[Any], Any]) -> Any:
    payload = await tmx.query(op, text, variables)
    try:
        return parse(payload)
    except tmx.Unexpected as e:
        raise net.FetchError(tmx.URL, 200, f"TMX {e}") from e


async def fetch_profile(symbol: str) -> dict | None:
    """Company (or fund) profile of a Canadian listing; None when TMX does not know it."""
    return await _ask("getQuoteBySymbol", PROFILE_QUERY, {"symbol": tmx.ticker(symbol), "locale": "en"}, parse_profile)


async def fetch_news(symbol: str) -> list[dict]:
    """Latest NEWS_LIMIT press releases of a Canadian listing."""
    t = tmx.ticker(symbol)
    return await _ask("getNewsForSymbol", NEWS_QUERY, {"symbol": t, "page": 1, "limit": NEWS_LIMIT, "locale": "en"},
                      lambda p: parse_news(p, t))


async def fetch_insiders(symbol: str) -> dict:
    """TMX's insider-trading totals and the SEDI reports of the last INSIDER_MONTHS months (two requests, one after
    the other)."""
    t = tmx.ticker(symbol)
    summary = await tmx.query("getCompanyInsidersActivities", INSIDER_SUMMARY_QUERY, {"symbol": t})
    return await _ask("getInsiderTransactions", INSIDER_TRANSACTIONS_QUERY,
                      {"symbol": t, "monthDuration": INSIDER_MONTHS}, lambda p: parse_insiders(summary, p))
