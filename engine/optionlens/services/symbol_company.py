"""Company payloads from cached source data (types.ts): Fundamentals, Insiders, NewsItem[], Profile, and the
peer-comparison ReportResult. The source is the one of the symbol's market (jobs.ondemand: Nasdaq for US listings;
TMX Money's profile, news and insider reports for Canadian ones). Indices have no company data (empty payloads)."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from ..analytics import stats
from ..jobs import ondemand
from ..repo import bars as bars_repo
from ..repo import symbols as symbols_repo
from ..analytics import markets
from . import context as ctx
from . import fundamentals as fundamentals_svc
from ..net import FetchError
from .errors import BadRequest, Unavailable

FIN_TITLES = {"income": "Income statement", "balance": "Balance sheet", "cashflow": "Cash flow",
              "ratios": "Financial ratios"}
FIN_UNIT = 1000.0        # Nasdaq statement values are in $ thousands
KEY_STAT_LABELS = {
    "Exchange": "Exchange", "Sector": "Sector", "Industry": "Industry", "OneYrTarget": "1y target",
    "TodayHighLow": "Today high / low", "ShareVolume": "Volume", "AverageVolume": "Avg volume",
    "PreviousClose": "Previous close", "FiftTwoWeekHighLow": "52w high / low", "MarketCap": "Market cap",
    "PERatio": "P/E", "ForwardPE1Yr": "Forward P/E (1y)", "EarningsPerShare": "EPS (TTM)",
    "AnnualizedDividend": "Annual dividend", "ExDividendDate": "Ex-dividend date",
    "DividendPaymentDate": "Dividend pay date", "Yield": "Dividend yield", "Beta": "Beta",
}
PEERS = 10
# Nasdaq summary figures with an OptionLens key stat (services.fundamentals) or a merged dividend: not repeated here,
# so every page shows one value of each.
OWN_FIGURES = frozenset({"MarketCap", "PERatio", "EarningsPerShare", "Yield", "FiftTwoWeekHighLow", "AverageVolume",
                         "AnnualizedDividend", "ExDividendDate", "DividendPaymentDate"})


def _num(v: Any) -> float | None:
    """'$1,234' / '-$5' / '(7)' / '12.5%' -> float (percent sign kept as a plain number)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    digits = re.sub(r"[^0-9.]", "", s)
    if digits in ("", "."):
        return None
    x = float(digits)
    return -x if neg else x


# Tabs whose source failure is told in a plain sentence (kind -> how the sentence names the data).
PLAIN_FAILURE = {"insiders": "Insider data", "news": "News", "profile": "The company profile"}


def _cached(kind: str, symbol: str, **params: Any) -> tuple[Any, datetime | None]:
    """(normalized source payload, fetched_at) via the on-demand TTL cache. Unavailable for a PLAIN_FAILURE kind
    whose source failed with nothing stored."""
    try:
        payload = ctx.run_async(ondemand.fetch_cached, kind, symbol, **params)
    except FetchError as e:
        if kind not in PLAIN_FAILURE:
            raise
        raise Unavailable(f"{PLAIN_FAILURE[kind]} could not be loaded right now. Try again in a few minutes.") from e
    hit = ondemand.peek_cached(kind, symbol, **params)
    return payload, (hit[1] if hit else None)


def _company(symbol: str) -> dict:
    """symbols row; company data exists for stocks/ETFs only."""
    return ctx.ensure_known(symbol)


def _label(key: str) -> str:
    return KEY_STAT_LABELS.get(key) or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", key)


# ----------------------------------------------------------------------------- fundamentals
def _fin_table(name: str, t: dict | None) -> dict | None:
    if not t or not t.get("rows"):
        return None
    return {"title": FIN_TITLES[name], "periods": list(t["periods"]),
            "rows": [{"label": r["label"], "values": list(r["values"])} for r in t["rows"]]}


def _row_values(table: dict | None, label: str) -> list[float | None]:
    for r in (table or {}).get("rows", []):
        if r["label"] == label:
            return [_num(v) for v in r["values"]]
    return []


def trailing_fcf(cashflow: dict | None, quarterly: bool) -> float | None:
    """Trailing-year free cash flow ($) = operating cash flow + capital expenditures (negative):
    latest annual column, or the sum of the latest 4 quarters."""
    ocf, capex = _row_values(cashflow, "Net Cash Flow-Operating"), _row_values(cashflow, "Capital Expenditures")
    n = 4 if quarterly else 1
    if len(ocf) < n or any(v is None for v in ocf[:n]):
        return None
    cap = sum(v or 0.0 for v in capex[:n])
    return (sum(ocf[:n]) + cap) * FIN_UNIT


def fundamentals(symbol: str, period: str = "annual") -> dict:
    if period not in ("annual", "quarterly"):
        raise BadRequest("period must be annual or quarterly")
    row = _company(symbol)
    sym = row["symbol"]
    out = {"symbol": sym, "period": period, "key_stats": [], "income": None, "balance": None, "cashflow": None,
           "ratios": None, "payback_years": None, "fetched_at": None}
    if row["kind"] == "index":
        return out
    summ, fetched = _cached("summary", sym)
    fin, fin_fetched = _cached("financials", sym, quarterly=period == "quarterly")   # None for ETFs
    fetched = fin_fetched or fetched
    out["key_stats"] = [{"label": _label(k), "value": None if v is None else str(v)} for k, v in (summ or {}).items()
                        if k not in OWN_FIGURES]
    for name in FIN_TITLES:
        out[name] = _fin_table(name, (fin or {}).get(name))
    q = ctx.quote_dict(sym)
    mcap = fundamentals_svc.for_symbol(row, q["price"] if q else None, ctx.today())["market_cap"]
    fcf = trailing_fcf((fin or {}).get("cashflow"), period == "quarterly")
    out["payback_years"] = mcap / fcf if mcap and fcf and fcf > 0 else None
    out["fetched_at"] = fetched
    return out


# ----------------------------------------------------------------------------- insiders / news
def _fmt_count(v: float | None) -> str | None:
    return None if v is None else f"{v:,.0f}"


def insiders(symbol: str) -> dict:
    row = _company(symbol)
    sym = row["symbol"]
    source = ondemand.source_of("insiders", sym)
    if row["kind"] != "stock":
        return {"symbol": sym, "source": source, "summary": [], "trades": []}
    data, _ = _cached("insiders", sym)
    data = data or {}
    summary = [{"label": r["label"], "m3": _fmt_count(r.get("months3")), "m12": _fmt_count(r.get("months12"))}
               for part in ("trades", "shares") for r in (data.get("summary") or {}).get(part, [])]
    trades = [{"insider": t.get("insider") or "", "relation": t.get("relation"), "date": ctx.as_date(t.get("date")),
               "transaction": t.get("transaction"), "shares": t.get("shares"), "price": t.get("price"),
               "value": t["shares"] * t["price"] if t.get("shares") is not None and t.get("price") is not None else None,
               "shares_held": t.get("shares_held")} for t in data.get("trades") or []]
    return {"symbol": sym, "source": source, "summary": summary, "trades": trades}


def news(symbol: str) -> list[dict]:
    row = _company(symbol)
    if row["kind"] == "index":
        return []
    items, _ = _cached("news", row["symbol"])
    out = []
    for n in items or []:
        created = n.get("created")
        out.append({"title": n.get("title") or "", "url": n.get("url") or "", "source": n.get("publisher"),
                    "published": (created.isoformat() if isinstance(created, date) else created) or n.get("ago"),
                    "summary": None})
    return out


# ----------------------------------------------------------------------------- profile / peers
def _peer_rows(row: dict) -> list[dict]:
    if not row.get("industry"):
        return []
    return [r for r in symbols_repo.by_industry(row["industry"], PEERS + 1) if r["symbol"] != row["symbol"]][:PEERS]


def profile(symbol: str) -> dict:
    row = _company(symbol)
    sym = row["symbol"]
    prof = {}
    if row["kind"] != "index":            # None (negatively cached) for ETFs
        prof, _ = _cached("profile", sym)
        prof = prof or {}
    summ = ondemand.peek_cached("summary", sym)
    exchange = prof.get("Exchange") or ((summ[0] or {}).get("Exchange") if summ else None)
    return {"symbol": sym, "name": prof.get("CompanyName") or row["name"] or sym,
            "description": prof.get("CompanyDescription"), "website": prof.get("CompanyUrl"),
            "sector": row["sector"] or prof.get("Sector"), "industry": row["industry"] or prof.get("Industry"),
            "exchange": exchange,
            "peers": [{"symbol": r["symbol"], "name": r["name"] or r["symbol"], "kind": r["kind"],
                       **markets.describe(r["symbol"])} for r in _peer_rows(row)]}


PEER_COLUMNS = [
    {"key": "symbol", "label": "Symbol", "fmt": "symbol"}, {"key": "name", "label": "Name", "fmt": "text"},
    {"key": "market_cap", "label": "Market cap", "fmt": "bigmoney"}, {"key": "price", "label": "Price", "fmt": "money"},
    {"key": "change_pct", "label": "% Chg", "fmt": "pct_signed"},
    {"key": "ret_1m", "label": "1m", "fmt": "pct_signed", "group": "Performance"},
    {"key": "ret_3m", "label": "3m", "fmt": "pct_signed", "group": "Performance"},
    {"key": "ret_ytd", "label": "YTD", "fmt": "pct_signed", "group": "Performance"},
    {"key": "ret_1y", "label": "1y", "fmt": "pct_signed", "group": "Performance"},
    {"key": "iv30", "label": "IV30", "fmt": "vol"}, {"key": "iv_rank", "label": "IV rank", "fmt": "pct"},
    {"key": "pe", "label": "P/E", "fmt": "num2", "description": "Price / EPS (Questrade's daily symbol info)"},
    {"key": "fundamentals_source", "label": "Fundamentals source", "fmt": "text", "default_hidden": True},
]


def peers(symbol: str) -> dict:
    row = _company(symbol)
    group = [row] + _peer_rows(row)
    syms = [r["symbol"] for r in group]
    closes = bars_repo.last_closes(260, syms)
    ranks = ctx.iv_rank_all()
    base = []
    for r in group:
        s = r["symbol"]
        c = closes.get(s)
        ret = stats.returns_row([ctx.as_date(d) for d in c["date"]], c["close"]) if c is not None and len(c["date"]) else {}
        q = ctx.quote_dict(s)
        price = q["price"] if q else (float(c["close"][-1]) if c is not None and len(c["close"]) else None)
        base.append((r, q, ret, price))
    ks = fundamentals_svc.for_symbols({r["symbol"]: p for r, _, _, p in base},
                                      {r["symbol"]: r["market_cap"] for r, _, _, _ in base}, ctx.today())
    rows = []
    for r, q, ret, price in base:
        s, k = r["symbol"], ks[r["symbol"]]
        rows.append({"symbol": s, "name": r["name"], "market_cap": k["market_cap"], "price": price,
                     "change_pct": q["change_pct"] if q else ret.get("d1"),
                     "ret_1m": ret.get("m1"), "ret_3m": ret.get("m3"), "ret_ytd": ret.get("ytd"), "ret_1y": ret.get("y1"),
                     "iv30": (ranks.get(s) or {}).get("iv30"), "iv_rank": (ranks.get(s) or {}).get("iv_rank"),
                     "pe": k["pe"], "fundamentals_source": k["sources"].get("pe") or k["sources"].get("market_cap")})
    notes = ["Peers = same industry, largest market caps (symbols table)."]
    if not row.get("industry"):
        notes.append("No industry known for this symbol: no peers.")
    return {"id": "peers", "title": f"{row['symbol']} peers", "asof": ctx.options_asof(row["symbol"]),
            "params": {"symbol": row["symbol"]}, "columns": PEER_COLUMNS, "rows": rows, "truncated": False,
            "notes": notes}

