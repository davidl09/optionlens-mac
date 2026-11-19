"""Nasdaq public JSON API -> models / lightly normalized dicts.

Parsers (`parse_*`) are pure; `fetch_*` do I/O via net. Nasdaq signals unknown symbols with HTTP 200
and `data: null`; fetchers return None/[] in that case.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from ..models import Bar, Dividend, EarningsEvent, Timing, normalize_symbol
from .. import net

BASE = "https://api.nasdaq.com/api"
AssetClass = Literal["stocks", "etf"]
_NUM = re.compile(r"[^0-9.+-]")
_TIMING: dict[str, Timing] = {"time-pre-market": "bmo", "time-after-hours": "amc"}


# ----------------------------------------------------------------------------- scalar parsing
def num(v: Any) -> float | None:
    """'$1,234.5' / '(0.08)' / '2.9%' / 12 -> float; 'N/A', '--', '' and None -> None. '%' is NOT scaled."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = _NUM.sub("", s)
    if s in ("", "-", "+", ".", "-."):
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def integer(v: Any) -> int | None:
    """Like num() but rounded to int."""
    x = num(v)
    return None if x is None else int(round(x))


def pct(v: Any) -> float | None:
    """Percent string/number -> fraction ('5.56' -> 0.0556)."""
    x = num(v)
    return None if x is None else x / 100.0


def day(v: Any) -> date | None:
    """'MM/DD/YYYY', 'M/D/YYYY', 'YYYY-MM-DD' or 'Nov 12, 2026' -> date; junk -> None."""
    if not v or not isinstance(v, str):
        return None
    s = v.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "N/A", "--") else s


def _data(payload: Any) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


# ----------------------------------------------------------------------------- parsers -> models
def parse_history(payload: dict, symbol: str) -> list[Bar]:
    """historical endpoint -> bars ascending by date (rows without a close are dropped)."""
    rows = ((_data(payload) or {}).get("tradesTable") or {}).get("rows") or []
    out: dict[date, Bar] = {}
    for r in rows:
        d, c = day(r.get("date")), num(r.get("close"))
        if d is None or c is None:
            continue
        out[d] = Bar(symbol, d, num(r.get("open")), num(r.get("high")), num(r.get("low")), c,
                     integer(r.get("volume")))
    return [out[d] for d in sorted(out)]


def parse_earnings(payload: dict, on: date) -> list[EarningsEvent]:
    """earnings calendar for one date -> events (surprise as fraction)."""
    rows = (_data(payload) or {}).get("rows") or []
    out = []
    for r in rows:
        sym = _text(r.get("symbol"))
        if not sym:
            continue
        out.append(EarningsEvent(
            symbol=normalize_symbol(sym), date=on,
            timing=_TIMING.get(r.get("time") or "", "unknown"),
            fiscal_quarter=_text(r.get("fiscalQuarterEnding")),
            eps_actual=num(r.get("eps")), eps_estimate=num(r.get("epsForecast")),
            surprise_pct=pct(r.get("surprise")), num_estimates=integer(r.get("noOfEsts")),
        ))
    return out


def parse_dividend_calendar(payload: dict) -> list[Dividend]:
    """dividend calendar -> dividends (kind 'cash')."""
    rows = ((_data(payload) or {}).get("calendar") or {}).get("rows") or []
    out = []
    for r in rows:
        sym, ex, amt = _text(r.get("symbol")), day(r.get("dividend_Ex_Date")), num(r.get("dividend_Rate"))
        if sym and ex and amt is not None:
            out.append(Dividend(normalize_symbol(sym), ex, amt, day(r.get("payment_Date")),
                                day(r.get("record_Date")), day(r.get("announcement_Date")), "cash"))
    return out


def parse_dividend_history(payload: dict, symbol: str) -> list[Dividend]:
    """per-symbol dividend history -> dividends (kind = feed type lower-cased, e.g. 'cash')."""
    rows = (((_data(payload) or {}).get("dividends")) or {}).get("rows") or []
    out = []
    for r in rows:
        ex, amt = day(r.get("exOrEffDate")), num(r.get("amount"))
        if ex and amt is not None:
            out.append(Dividend(symbol, ex, amt, day(r.get("paymentDate")), day(r.get("recordDate")),
                                day(r.get("declarationDate")), (_text(r.get("type")) or "cash").lower()))
    return out


def parse_stock_screener(payload: dict) -> list[dict]:
    """stock screener -> [{symbol, name, market_cap, sector, industry, country, price, volume}]."""
    rows = (_data(payload) or {}).get("rows") or []
    return [{
        "symbol": normalize_symbol(r["symbol"]), "name": (r.get("name") or "").strip(),
        "market_cap": num(r.get("marketCap")), "sector": _text(r.get("sector")),
        "industry": _text(r.get("industry")), "country": _text(r.get("country")),
        "price": num(r.get("lastsale")), "volume": integer(r.get("volume")),
    } for r in rows if r.get("symbol")]


def parse_etf_screener(payload: dict) -> list[dict]:
    """ETF screener -> [{symbol, name, price}]."""
    rows = (((_data(payload) or {}).get("data")) or {}).get("rows") or []
    return [{"symbol": normalize_symbol(r["symbol"]), "name": (r.get("companyName") or "").strip(),
             "price": num(r.get("lastSalePrice"))} for r in rows if r.get("symbol")]


# ----------------------------------------------------------------------------- parsers -> dicts
def _labels(d: dict | None) -> dict[str, Any]:
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (d or {}).items()}


def parse_info(payload: dict) -> dict | None:
    """quote info -> {symbol, name, stock_type, exchange, asset_class, price, change, change_pct, volume, market_status}."""
    d = _data(payload)
    if not d:
        return None
    p = d.get("primaryData") or {}
    return {
        "symbol": normalize_symbol(d.get("symbol") or ""), "name": _text(d.get("companyName")),
        "stock_type": _text(d.get("stockType")), "exchange": _text(d.get("exchange")),
        "asset_class": (d.get("assetClass") or "").lower() or None,
        "price": num(p.get("lastSalePrice")), "change": num(p.get("netChange")),
        "change_pct": pct(p.get("percentageChange")), "volume": integer(p.get("volume")),
        "market_status": _text(d.get("marketStatus")),
    }


def parse_summary(payload: dict) -> dict | None:
    """quote summary -> {Key: raw value string} (e.g. Sector, Industry, MarketCap, OneYrTarget, PERatio)."""
    d = _data(payload)
    return _labels(d.get("summaryData")) if d else None


def parse_profile(payload: dict) -> dict | None:
    """company profile -> {Key: value} (CompanyName, Sector, Industry, CompanyDescription, CompanyUrl ...)."""
    d = _data(payload)
    return _labels(d) if d else None


_FIN_TABLES = {"incomeStatementTable": "income", "balanceSheetTable": "balance",
               "cashFlowTable": "cashflow", "financialRatiosTable": "ratios"}


def parse_financials(payload: dict) -> dict | None:
    """financials -> {income|balance|cashflow|ratios: {periods: [str], rows: [{label, values: [str|None]}]}}; values raw."""
    d = _data(payload)
    if not d:
        return None
    out = {}
    for src, name in _FIN_TABLES.items():
        t = d.get(src) or {}
        hdr = t.get("headers") or {}
        keys = [k for k in hdr if k != "value1"]
        out[name] = {
            "periods": [hdr[k] for k in keys],
            "rows": [{"label": r.get("value1"), "values": [_text(r.get(k)) for k in keys]}
                     for r in t.get("rows") or []],
        }
    return out


def parse_insiders(payload: dict) -> dict | None:
    """insider trades -> {summary: {trades: [...], shares: [...]}, trades: [{insider, relation, date, transaction, own_type, shares, price, shares_held}]}."""
    d = _data(payload)
    if not d:
        return None
    rows = (((d.get("transactionTable") or {}).get("table")) or {}).get("rows") or []

    def summary(t):
        return [{"label": r.get("insiderTrade"), "months3": num(r.get("months3")),
                 "months12": num(r.get("months12"))} for r in (t or {}).get("rows") or []]

    return {
        "summary": {"trades": summary(d.get("numberOfTrades")), "shares": summary(d.get("numberOfSharesTraded"))},
        "trades": [{
            "insider": _text(r.get("insider")), "relation": _text(r.get("relation")),
            "date": day(r.get("lastDate")), "transaction": _text(r.get("transactionType")),
            "own_type": _text(r.get("ownType")), "shares": integer(r.get("sharesTraded")),
            "price": num(r.get("lastPrice")), "shares_held": integer(r.get("sharesHeld")),
        } for r in rows],
    }


def parse_news(payload: dict) -> list[dict]:
    """news -> [{id, title, url (absolute), publisher, created (date|None), ago, symbols}]."""
    rows = (_data(payload) or {}).get("rows") or []
    out = []
    for r in rows:
        url = r.get("url") or ""
        out.append({
            "id": r.get("id"), "title": _text(r.get("title")),
            "url": url if url.startswith("http") else f"https://www.nasdaq.com{url}",
            "publisher": _text(r.get("publisher")), "created": day(r.get("created")),
            "ago": _text(r.get("ago")),
            "symbols": [s.split("|")[0].upper() for s in r.get("related_symbols") or []],
        })
    return out


_CONSOLIDATED = re.compile(r"\$?([\d,.]+)\s+([+-]?[\d,.]+)\s+\(([+-]?[\d,.]+)%\)")


def parse_extended(payload: dict) -> dict | None:
    """extended-trading -> {price, change, change_pct (fraction), volume, high, low, updated} or None."""
    d = _data(payload)
    rows = ((d or {}).get("infoTable") or {}).get("rows") or []
    if not rows:
        return None
    r = rows[0]
    m = _CONSOLIDATED.search(r.get("consolidated") or "")
    upd = d.get("lastUpdateInfo")
    return {
        "price": num(m.group(1)) if m else num(r.get("consolidated")),
        "change": num(m.group(2)) if m else None,
        "change_pct": pct(m.group(3)) if m else None,
        "volume": integer(r.get("volume")),
        "high": num((r.get("highPrice") or "").split("(")[0]),
        "low": num((r.get("lowPrice") or "").split("(")[0]),
        "updated": (upd[0] if isinstance(upd, list) and upd else upd) or None,
    }


def parse_earnings_surprise(payload: dict) -> list[dict]:
    """earnings-surprise -> [{fiscal_quarter, date, eps, consensus, surprise_pct (fraction)}] newest first."""
    rows = (((_data(payload) or {}).get("earningsSurpriseTable")) or {}).get("rows") or []
    return [{"fiscal_quarter": _text(r.get("fiscalQtrEnd")), "date": day(r.get("dateReported")),
             "eps": num(r.get("eps")), "consensus": num(r.get("consensusForecast")),
             "surprise_pct": pct(r.get("percentageSurprise"))} for r in rows]


# ----------------------------------------------------------------------------- fetchers
async def _get(path: str, **params: Any) -> dict:
    return await net.get_json(f"{BASE}{path}", params or None)


async def fetch_history(symbol: str, asset: AssetClass, start: date, end: date) -> list[Bar]:
    """Daily bars in [start, end]."""
    p = await _get(f"/quote/{symbol}/historical", assetclass=asset, fromdate=start.isoformat(),
                   todate=end.isoformat(), limit=9999)
    return parse_history(p, symbol)


async def fetch_earnings(on: date) -> list[EarningsEvent]:
    """Earnings calendar for one date."""
    return parse_earnings(await _get("/calendar/earnings", date=on.isoformat()), on)


async def fetch_dividend_calendar(on: date) -> list[Dividend]:
    """Dividend calendar (ex-dates) for one date."""
    return parse_dividend_calendar(await _get("/calendar/dividends", date=on.isoformat()))


async def fetch_dividend_history(symbol: str, asset: AssetClass) -> list[Dividend]:
    """Full dividend history for a symbol."""
    return parse_dividend_history(await _get(f"/quote/{symbol}/dividends", assetclass=asset), symbol)


async def fetch_stock_screener() -> list[dict]:
    """All listed stocks with market cap."""
    return parse_stock_screener(await _get("/screener/stocks", tableonly="true", limit=10000, download="true"))


async def fetch_etf_screener() -> list[dict]:
    """All listed ETFs."""
    return parse_etf_screener(await _get("/screener/etf", tableonly="true", limit=10000, download="true"))


async def fetch_info(symbol: str, asset: AssetClass = "stocks") -> dict | None:
    """Quote info (None for unknown symbol/asset class)."""
    return parse_info(await _get(f"/quote/{symbol}/info", assetclass=asset))


async def fetch_summary(symbol: str, asset: AssetClass = "stocks") -> dict | None:
    """Quote summary key/values."""
    return parse_summary(await _get(f"/quote/{symbol}/summary", assetclass=asset))


async def fetch_profile(symbol: str) -> dict | None:
    """Company profile key/values."""
    return parse_profile(await _get(f"/company/{symbol}/company-profile"))


async def fetch_financials(symbol: str, quarterly: bool = False) -> dict | None:
    """Financial statements (annual or quarterly)."""
    return parse_financials(await _get(f"/company/{symbol}/financials", frequency=2 if quarterly else 1))


async def fetch_insiders(symbol: str) -> dict | None:
    """Insider trades (latest 50)."""
    return parse_insiders(await _get(f"/company/{symbol}/insider-trades", limit=50, type="ALL",
                                     sortColumn="lastDate", sortOrder="DESC"))


async def fetch_news(symbol: str, asset: AssetClass = "stocks") -> list[dict]:
    """Latest 20 headlines."""
    return parse_news(await _get("/news/topic/articlebysymbol", q=f"{symbol.lower()}|{asset}",
                                 offset=0, limit=20, fallback="true"))


async def fetch_extended(symbol: str, market: Literal["pre", "post"], asset: AssetClass = "stocks") -> dict | None:
    """Pre-market or after-hours last trade summary."""
    return parse_extended(await _get(f"/quote/{symbol}/extended-trading", markettype=market,
                                     assetclass=asset, time=0))


async def fetch_earnings_surprise(symbol: str) -> list[dict]:
    """Last 4 quarters EPS vs consensus."""
    return parse_earnings_surprise(await _get(f"/company/{symbol}/earnings-surprise"))
