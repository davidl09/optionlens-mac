"""API test app over a temporary DATA_DIR seeded from the recorded fixtures (network disabled)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from optionlens import config, db, net
from optionlens.jobs import chains
from optionlens.models import Bar, EarningsEvent
from optionlens.repo import bars, cache, dividends, earnings, market, options, symbols
from optionlens.services import context
from optionlens.sources import cboe, nasdaq

FIX = Path(__file__).resolve().parents[1] / "fixtures"
FETCHED = datetime(2026, 11, 19, 19, 3, tzinfo=timezone.utc)


def _json(name: str):
    return json.loads((FIX / name).read_text())


def seed() -> None:
    symbols.upsert_symbols([
        {"symbol": "AAPL", "name": "Apple Inc.", "kind": "stock", "sector": "Technology",
         "industry": "Computer Manufacturing", "market_cap": 5.07e12},
        {"symbol": "DELL", "name": "Dell Technologies", "kind": "stock", "sector": "Technology",
         "industry": "Computer Manufacturing", "market_cap": 9e10},
        {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "kind": "etf"},
    ])
    symbols.set_universe(["AAPL", "SPY"])
    hist = nasdaq.parse_history(_json("nasdaq_history_AAPL.json"), "AAPL")
    bars.upsert_bars(hist)
    bars.upsert_bars([Bar("SPY", b.date, b.open, b.high, b.low, b.close * 1.9, b.volume) for b in hist])
    bars.upsert_bars([Bar("DELL", b.date, b.open, b.high, b.low, b.close / 2.5, b.volume) for b in hist])
    snap = cboe.parse_chain(_json("cboe_chain_AAPL.json"), "AAPL", fetched_at=FETCHED)
    chains.store_chain(snap, FETCHED)
    options.write_option_eod(snap.quote.ts.date() - timedelta(days=1), FETCHED)  # a prior EOD day (OI change)
    dividends.upsert_dividends(nasdaq.parse_dividend_history(_json("nasdaq_dividends_AAPL.json"), "AAPL"))
    last = hist[-1].date
    events = [EarningsEvent("AAPL", d, "amc", "Sep/2026", 2.0, 1.9, 0.05, 30)
              for d in (last - timedelta(days=91), last - timedelta(days=182), last - timedelta(days=273))]
    events.append(EarningsEvent("AAPL", date(2027, 1, 28), "amc", "Dec/2026", None, 2.5, None, 30))
    earnings.upsert_earnings(events)
    market.upsert_rates([(date(2026, 11, 18), 0.0426)])
    cache.cache_put("summary:AAPL", nasdaq.parse_summary(_json("nasdaq_summary_AAPL.json")))
    cache.cache_put("profile:AAPL", nasdaq.parse_profile(_json("nasdaq_profile_AAPL.json")))
    cache.cache_put("insiders:AAPL", nasdaq.parse_insiders(_json("nasdaq_insiders_AAPL.json")))
    cache.cache_put("news:AAPL", nasdaq.parse_news(_json("nasdaq_news_AAPL.json")))
    for q in (False, True):
        cache.cache_put(f"financials:AAPL:quarterly={q}", nasdaq.parse_financials(_json("nasdaq_financials_AAPL.json")))


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    mp = pytest.MonkeyPatch()
    mp.setenv("OPTIONLENS_DATA_DIR", str(data))
    mp.setenv("OPTIONLENS_NO_SCHEDULER", "1")

    async def offline(url, params=None):
        if url.endswith("/info"):          # Nasdaq answers unknown symbols with data: null
            return httpx.Response(200, json={"data": None}, request=httpx.Request("GET", url))
        raise net.FetchError(url, None, "network disabled in tests")

    mp.setattr(net, "get", offline)
    config.get_settings.cache_clear()
    db.close()
    db.connect(data / "optionlens.duckdb")
    seed()
    db.close()
    context.invalidate()
    from optionlens.api.app import create_app
    with TestClient(create_app(web_dist=data / "no-dist")) as c:
        yield c
    context.invalidate()
    mp.undo()
    config.get_settings.cache_clear()
