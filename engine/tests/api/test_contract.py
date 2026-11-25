"""Contract: every endpoint owned by the application layer returns JSON whose keys equal the
types.ts interfaces (recursively through inline objects, arrays, intersections and references)."""
from __future__ import annotations

import pytest

from .tsschema import load_types, mismatches

TYPES = load_types()
S = "/api/symbol/AAPL"
OSI = "AAPL261218C00350000"

GETS = [
    (f"{S}/summary", "SymbolSummary"), (f"{S}/bars?range=1y", "Bar[]"), (f"{S}/price-action", "PriceAction"),
    (f"{S}/chain", "Chain"), (f"{S}/activity", "Activity"), (f"{S}/activity?expiry=all", "Activity"),
    (f"{S}/open-interest", "OpenInterest"), (f"{S}/contract/{OSI}", "ContractHistory"),
    (f"{S}/volatility", "Volatility"), (f"{S}/skew", "Skew"),
    (f"{S}/iv-surface", "IvSurface"), (f"{S}/iv-surface?rows=strike&mode=chg_1d", "IvSurface"), (f"{S}/implied-moves", "ImpliedMoves"),
    (f"{S}/vol-seasonality", "VolSeasonality"), (f"{S}/earnings", "Earnings"),
    (f"{S}/earnings/strategies?lookback=4&entry=-1&exit=1", "EarningsStrategyResult"),
    (f"{S}/earnings/strategies/long-straddle/trades?lookback=4&entry=-1&exit=1", "BacktestTrade[]"),
    (f"{S}/dividends", "Dividends"), (f"{S}/seasonality", "Seasonality"), (f"{S}/events", "Events"),
    (f"{S}/straddle", "StraddleInfo"), (f"{S}/ideas", "TradeIdeas"),
    (f"{S}/fundamentals?period=annual", "Fundamentals"), (f"{S}/fundamentals?period=quarterly", "Fundamentals"),
    (f"{S}/peers", "ReportResult"), (f"{S}/insiders", "Insiders"), (f"{S}/news", "NewsItem[]"),
    (f"{S}/profile", "Profile"), ("/api/strategy-templates", "StrategyTemplate[]"), ("/api/status", "Status"),
    ("/api/search?q=AA", "SymbolRef[]"), ("/api/compare/volatility?symbols=AAPL,SPY", "CompareVolatility"),
]
CALC = {"symbol": "AAPL", "legs": [{"kind": "option", "right": "C", "strike": 350, "expiry": "2026-12-18", "qty": 1},
                                   {"kind": "stock", "qty": 100}]}
STOCK_BT = {"symbols": ["AAPL"], "entry": {"type": "every_n_days", "n": 20}, "exit": {"hold_days": 5}, "side": "long"}
OPT_BT = {"symbol": "AAPL", "strategy": "long-straddle", "strike_rule": {"type": "atm"}, "entry": {"type": "monthly"},
          "target_dte": 30, "exit": {"type": "expiry"}, "start": "2025-01-01"}
POSTS = [("/api/calc/strategy", CALC, "CalcResult"), ("/api/backtest/stock", STOCK_BT, "BacktestResult"),
         ("/api/backtest/options", OPT_BT, "BacktestResult")]


def _schema(name: str) -> tuple:
    return TYPES.parse(name)


@pytest.mark.parametrize("url,iface", GETS)
def test_get_matches_types_ts(client, url, iface):
    r = client.get(url)
    assert r.status_code == 200, r.text
    assert mismatches(r.json(), _schema(iface)) == []


@pytest.mark.parametrize("url,body,iface", POSTS)
def test_post_matches_types_ts(client, url, body, iface):
    r = client.post(url, json=body)
    assert r.status_code == 200, r.text
    assert mismatches(r.json(), _schema(iface)) == []


def test_extractor_reads_real_interfaces():
    assert TYPES.interface_fields("Bar") == {"date", "open", "high", "low", "close", "volume", "sma20", "sma50", "sma200"}
    oi = TYPES.parse("OpenInterest")[1]["top_increases"][0]
    assert oi[0] == "arr" and {"oi_prev", "oi_change", "osi", "itm"} <= set(oi[1][1])
    assert mismatches({"date": "x", "open": 1}, TYPES.parse("Bar")) != []
