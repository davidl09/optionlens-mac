"""Seeded database for the report / user / alert tests (application layer, part 2).

Built from the recorded AAPL CBOE chain with expiries shifted (whole weeks) so DTE windows hold on
any test date; synthetic but realistic daily bars; 25 days of stored metrics history (IV rank,
average option volume); earnings, dividends, a prior EOD snapshot with OI changes, a UOV snapshot
and a watchlist. The fixture swaps the process-wide DuckDB connection and restores the previous
one afterwards (other API tests share a session-scoped connection)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pytest

from optionlens import db, net
from optionlens.analytics.calendar import NY, add_trading_days, is_trading_day, now_ny, prev_trading_day
from optionlens.jobs import chains, ondemand
from optionlens.jobs._util import session_date
from optionlens.models import Bar, ChainSnapshot, Dividend, EarningsEvent
from optionlens.repo import bars, dividends, earnings, market, metrics, options, report_snapshots, symbols, user
from optionlens.services import context, market as market_svc
from optionlens.services.reports import universe
from optionlens.sources import cboe

FIX = Path(__file__).resolve().parents[1] / "fixtures"
FIX_DAY = date(2026, 11, 19)
HIST_DAYS = 25


def _chain(symbol: str, now: datetime, scale: float = 1.0) -> ChainSnapshot:
    snap = cboe.parse_chain(json.loads((FIX / "cboe_chain_AAPL.json").read_text()), "AAPL", fetched_at=now)
    shift = timedelta(days=max(0, (now.astimezone(NY).date() - FIX_DAY).days // 7 * 7))
    today = now.astimezone(NY).date()
    cs = []
    for c in snap.contracts:
        e = c.expiry + shift
        if e < today:
            continue
        osi = symbol + c.osi[len("AAPL"):] if symbol != "AAPL" else c.osi
        cs.append(replace(c, symbol=symbol, osi=osi, expiry=e, strike=c.strike * scale,
                          bid=None if c.bid is None else c.bid * scale, ask=None if c.ask is None else c.ask * scale,
                          last=None if c.last is None else c.last * scale))
    q = replace(snap.quote, symbol=symbol, ts=now, price=snap.quote.price * scale,
                prev_close=(snap.quote.prev_close or snap.quote.price) * scale,
                open=(snap.quote.open or snap.quote.price) * scale, high=(snap.quote.high or snap.quote.price) * scale,
                low=(snap.quote.low or snap.quote.price) * scale)
    return ChainSnapshot(q, tuple(cs), now)


def _bars(symbol: str, last_close: float, end: date, n: int = 1600, seed: int = 1, vol: float = 0.018) -> list[Bar]:
    days = []
    d = end
    while len(days) < n:
        if is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0004, vol, n)
    closes = last_close * np.exp(np.cumsum(r) - np.cumsum(r)[-1])
    out = []
    for i, (dd, c) in enumerate(zip(days, closes)):
        o = closes[i - 1] * (1 + rng.normal(0, 0.004)) if i else c
        out.append(Bar(symbol, dd, float(o), float(max(o, c) * 1.006), float(min(o, c) * 0.994), float(c),
                       int(5e7 * (1 + 0.3 * rng.random()))))
    return out


def seed() -> dict:
    now = datetime.now(timezone.utc)
    today = now_ny().date()
    symbols.upsert_symbols([
        {"symbol": "AAPL", "name": "Apple Inc.", "kind": "stock", "sector": "Technology",
         "industry": "Computer Manufacturing", "market_cap": 5.07e12},
        {"symbol": "XYZ", "name": "Xyz Realty Trust", "kind": "stock", "sector": "Real Estate",
         "industry": "Real Estate Investment Trusts", "market_cap": 8e10},
        {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "kind": "etf"},
    ])
    symbols.set_universe(["AAPL", "XYZ", "SPY"])
    aapl, xyz = _chain("AAPL", now), _chain("XYZ", now, 0.5)
    day = session_date(now)
    prev = prev_trading_day(day)
    for snap, seed_ in ((aapl, 1), (xyz, 2)):
        q = snap.quote
        bars.upsert_bars(_bars(q.symbol, q.prev_close or q.price, prev, seed=seed_))
    spy_bars = _bars("SPY", 700.0, prev, seed=3, vol=0.01)
    bars.upsert_bars(spy_bars)
    from optionlens.models import Quote
    from optionlens.repo import quotes
    quotes.upsert_quotes([Quote("SPY", now, 707.0, 7.0, 0.01, 701.0, 708.0, 699.0, 700.0, 9e7, 706.9, 707.1, 0.18)])
    for snap in (aapl, xyz):
        chains.store_chain(snap, now)
    # metrics history: IV30 drifting 0.20 -> 0.35 and low option volume -> today's rank / rel volume defined
    for sym in ("AAPL", "XYZ"):
        today_row = metrics.get_metrics(sym, day)
        cv, pv = today_row["call_volume"] or 0, today_row["put_volume"] or 0
        d = day
        for k in range(1, HIST_DAYS + 1):
            d = prev_trading_day(d)
            metrics.upsert_metrics(d, sym, {"iv30": 0.20 + 0.15 * (k % 7) / 7, "hv30": 0.22, "hv20": 0.21,
                                            "skew25_30": 0.03 + 0.001 * k, "call_volume": cv // 5,
                                            "put_volume": pv // 5, "close": None})
    # prior EOD snapshot with OI changes
    options.write_option_eod(prev, now - timedelta(minutes=5), ["AAPL"])
    with db.write() as cur:
        cur.execute("UPDATE option_eod SET oi = greatest(oi - 700, 0) WHERE date = ? AND symbol = 'AAPL' AND oi > 2000", [prev])
    # earnings: upcoming (AAPL amc in 3 days, XYZ bmo tomorrow-ish) + 8 past quarterly events
    up = add_trading_days(today, 3)
    events = [EarningsEvent("AAPL", up, "amc", "Dec/2026", None, 2.5, None, 30),
              EarningsEvent("XYZ", add_trading_days(today, 1), "bmo", "Dec/2026", None, 1.1, None, 12)]
    for k in range(1, 9):
        d = add_trading_days(prev, -63 * k + 10)
        events += [EarningsEvent("AAPL", d, "amc", f"Q{k}", 2.0 + 0.1 * k, 1.9, 0.05, 30),
                   EarningsEvent("XYZ", d, "bmo", f"Q{k}", 1.0, 1.1, -0.09, 10)]
    earnings.upsert_earnings(events)
    # dividends: quarterly regular, rising, plus a future ex-date
    divs = []
    for k in range(12):
        ex = add_trading_days(prev, -63 * k - 5)
        divs.append(Dividend("AAPL", ex, 0.26 - 0.005 * k, ex + timedelta(days=7), ex, ex - timedelta(days=10), "Cash"))
        divs.append(Dividend("XYZ", ex, 0.90, ex + timedelta(days=7), ex, ex - timedelta(days=10), "Cash"))
    nxt = add_trading_days(today, 6)
    divs += [Dividend("AAPL", nxt, 0.27, nxt + timedelta(days=7), nxt, today - timedelta(days=1), "Cash"),
             Dividend("XYZ", nxt, 0.95, nxt + timedelta(days=7), nxt, today - timedelta(days=2), "Cash")]
    dividends.upsert_dividends(divs)
    market.upsert_rates([(prev, 0.0426)])
    market.upsert_index_bars([Bar(s, b.date, None, None, None, v * (1 + 0.001 * i), None)
                              for s, v in (("VIX9D", 15.0), ("VIX", 17.0), ("VIX3M", 19.0), ("VIX6M", 20.0))
                              for i, b in enumerate(spy_bars[-260:])])
    report_snapshots.write_snapshots(prev, "unusual-option-volume",
                                     [{"symbol": "AAPL", "price": float(aapl.quote.prev_close), "rel_volume": 3.0}])
    wl = user.create_watchlist("Core", ["AAPL", "XYZ"])
    return {"now": now, "today": today, "day": day, "prev": prev, "watchlist_id": wl["id"],
            "aapl_spot": aapl.quote.price, "earnings_day": up}


def _clear_caches() -> None:
    context.invalidate()
    universe.invalidate()
    market_svc.invalidate()


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Seed a fresh DB (network disabled) and yield the seed facts; restores the prior connection."""
    prev_path = db.path()
    mp = pytest.MonkeyPatch()

    async def offline(url, params=None):
        if url.endswith("/info"):          # Nasdaq answers unknown symbols with data: null
            return httpx.Response(200, json={"data": None}, request=httpx.Request("GET", url))
        raise net.FetchError(url, None, "network disabled in tests")

    async def fake_cached(kind, symbol, **params):
        if kind == "extended":
            return {"price": 101.0, "change": 1.0, "change_pct": 0.01, "volume": 1000, "high": 102.0, "low": 100.0,
                    "updated": "08:30 ET"}
        raise net.FetchError(kind, None, "network disabled in tests")

    mp.setattr(net, "get", offline)
    mp.setattr(ondemand, "fetch_cached", fake_cached)
    db.close()
    db.connect(tmp_path_factory.mktemp("reports") / "optionlens.duckdb")
    _clear_caches()
    facts = seed()
    _clear_caches()
    yield facts
    db.close()
    _clear_caches()
    mp.undo()
    if prev_path is not None:
        db.connect(prev_path)


@pytest.fixture(scope="module")
def api(seeded):
    from fastapi.testclient import TestClient

    from optionlens.api.app import create_app
    return TestClient(create_app(web_dist=Path("/nonexistent")))   # no lifespan: keeps the seeded connection
