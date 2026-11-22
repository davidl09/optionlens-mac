"""Engine-specific API behaviour: bearer auth, open health, CORS, body limit, Status.sources/bridge and
the ingest API (docs/INTEGRATION.md §2, §4, §5, §6). Ingest writes use DELL / VIX so the shared AAPL
fixtures stay untouched, and only dates up to the seeded session (2026-11-19)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from optionlens.api.app import BodyLimit, create_app
from optionlens.config import get_settings

TOKEN = "t" * 32
ORIGIN = "app://optionlens"
SESSION = date(2026, 11, 19)                                  # the seeded metrics session
FETCHED = datetime(2026, 11, 19, 19, 30, tzinfo=timezone.utc)   # 14:30 ET, after the seeded chains


@pytest.fixture(scope="module")
def authed(client):
    """App with a token; shares the session client's open database (no lifespan of its own)."""
    return TestClient(create_app(replace(get_settings(), api_token=TOKEN)))


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------------- auth / CORS
def test_health_is_open_and_describes_the_engine(authed, client):
    h = authed.get("/api/health").json()
    assert h["ok"] is True and h["auth"] is True and set(h) == {"ok", "version", "auth", "server_id"}
    assert len(h["server_id"]) == 32 and client.get("/api/health").json()["server_id"] == h["server_id"]
    assert client.get("/api/health").json()["auth"] is False


@pytest.mark.parametrize("headers", [{}, _auth("wrong" * 8), {"Authorization": TOKEN},
                                     {"Authorization": f"Basic {TOKEN}"}])
def test_api_requires_the_bearer_token(authed, headers):
    for path in ("/api/status", "/api/symbol/AAPL/summary", "/api/ingest/wants", "/api/nope", "/"):
        r = authed.get(path, headers=headers)
        assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}, path
    assert authed.post("/api/ingest/heartbeat", json={}, headers=headers).status_code == 401


def test_valid_token_passes(authed):
    assert authed.get("/api/status", headers=_auth()).status_code == 200
    assert authed.get("/api/status", headers={"authorization": f"bearer {TOKEN}"}).status_code == 200
    assert authed.get("/api/nope", headers=_auth()).status_code == 404


def test_cors_preflight_needs_no_token_and_401s_carry_cors(authed):
    r = authed.options("/api/status", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET",
                                               "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ORIGIN
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in r.headers
    bad = authed.options("/api/status", headers={"Origin": "https://evil.example",
                                                 "Access-Control-Request-Method": "GET"})
    assert bad.status_code == 400
    r = authed.get("/api/status", headers={"Origin": ORIGIN})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == ORIGIN


def test_body_limit():
    inner = Starlette(routes=[Route("/", lambda req: PlainTextResponse("ok"), methods=["POST"])])
    c = TestClient(BodyLimit(inner, limit=10))
    assert c.post("/", content=b"x" * 10).text == "ok"
    r = c.post("/", content=b"x" * 11)
    assert r.status_code == 413 and "over 10 bytes" in r.json()["detail"]


# ----------------------------------------------------------------------------- status
def test_status_sources_and_bridge(client):
    client.post("/api/ingest/heartbeat", json={"client": "optionlens-mac 0.1.0", "ibkr_connected": True,
                                               "account_type": "paper"})
    st = client.get("/api/status").json()
    assert [s["name"] for s in st["sources"]] == ["nasdaq", "treasury", "ibkr-bridge"]   # no vendor configured
    assert st["bridge"]["connected"] is True and st["bridge"]["client"] == "optionlens-mac 0.1.0"
    ib = st["sources"][2]
    assert ib["ok"] is True and ib["detail"] == "optionlens-mac 0.1.0"
    assert st["bridge"]["farms"] is None and st["bridge"]["options_subscription"] is None   # an older bridge
    client.post("/api/ingest/heartbeat", json={"client": "optionlens-mac 0.1.7", "ibkr_connected": True,
                                               "account_type": "live", "options_subscription": "delayed_only",
                                               "farms": {"market_data": "ok", "historical": "ok", "sec_def": "down"}})
    br = client.get("/api/status").json()["bridge"]
    assert br["farms"] == {"market_data": "ok", "historical": "ok", "sec_def": "down"}
    assert br["options_subscription"] == "delayed_only"
    assert client.post("/api/ingest/heartbeat", json={"client": "x", "ibkr_connected": True,
                                                      "farms": {"sec_def": "maintenance"}}).status_code == 422
    hb = client.post("/api/ingest/heartbeat", json={"client": "x", "ibkr_connected": False}).json()
    assert datetime.fromisoformat(hb["server_time"]).tzinfo is not None
    assert client.get("/api/status").json()["bridge"]["connected"] is False


# ----------------------------------------------------------------------------- ingest
def _contract(strike: float, right: str, **kw) -> dict:
    return {"expiry": "2026-12-18", "strike": strike, "right": right, "bid": 1.0, "ask": 1.2, "last": 1.1,
            "volume": 10, "oi": 100, "iv": 0.3, "delta": 0.5 if right == "C" else -0.5, **kw}


def _chain(**kw) -> dict:
    return {"source": "ibkr", "delayed": False, "symbol": "dell", "fetched_at": FETCHED.isoformat(),
            "underlying": {"price": 120.0, "bid": 119.9, "ask": 120.1, "prev_close": 118.0, "volume": 1000},
            "contracts": [_contract(k, r) for k in (115.0, 120.0, 125.5) for r in ("C", "P")], **kw}


def test_ingest_chain_is_served_as_the_symbol_chain(client):
    r = client.post("/api/ingest/chain", json=_chain())
    assert r.status_code == 200, r.text
    assert r.json() == {"stored": 6, "symbol": "DELL"}
    assert client.post("/api/ingest/chain", json=_chain()).json()["stored"] == 6     # idempotent
    ch = client.get("/api/symbol/DELL/chain").json()
    assert ch["expiry"] == "2026-12-18" and ch["strikes"] == [115.0, 120.0, 125.5]
    assert [c["osi"] for c in ch["calls"]] == ["DELL261218C00115000", "DELL261218C00120000", "DELL261218C00125500"]
    assert ch["underlying"]["price"] == 120.0 and ch["underlying"]["change"] == pytest.approx(2.0)
    assert ch["delayed"] is False and ch["underlying"]["delayed"] is False
    from optionlens.services import ingest
    assert "DELL" in ingest.sweep_hooks.pending.get(SESSION, set())          # on_sweep_complete scheduled


def test_after_hours_chain_and_quote_are_labelled(client):
    """A chain fetched after its session's close (IBKR frozen data) says so on the chain and its quote; one fetched
    during the session does not."""
    assert client.post("/api/ingest/chain", json=_chain()).status_code == 200
    ch = client.get("/api/symbol/DELL/chain").json()
    assert ch["after_hours"] is False and ch["underlying"]["after_hours"] is False
    sat = "2026-11-21T17:00:00+00:00"                               # Saturday: Friday's closing values
    body = _chain(fetched_at=sat, underlying={"price": 120.0, "bid": 119.9, "ask": 120.1, "prev_close": 118.0,
                                              "volume": 1000, "ts": sat})
    assert client.post("/api/ingest/chain", json=body).status_code == 200
    ch = client.get("/api/symbol/DELL/chain").json()
    assert ch["after_hours"] is True and ch["underlying"]["after_hours"] is True
    assert client.post("/api/ingest/chain", json=_chain()).status_code == 200      # older: latest stays Saturday's
    assert client.get("/api/symbol/DELL/chain").json()["after_hours"] is True
    from optionlens import db                                       # leave DELL as the other tests expect it
    from optionlens.repo import metrics
    with db.write() as cur:
        for t in ("option_latest", "quotes_latest"):
            cur.execute(f"DELETE FROM {t} WHERE symbol = 'DELL'")
        cur.execute("DELETE FROM metrics_daily WHERE symbol = 'DELL' AND date = DATE '2026-11-20'")
    assert metrics.get_metrics("DELL", date(2026, 11, 20)) is None
    assert client.post("/api/ingest/chain", json=_chain()).status_code == 200


def test_ingest_delayed_flag_is_required_and_exposed(client):
    body = _chain()
    del body["delayed"]
    assert client.post("/api/ingest/chain", json=body).status_code == 422
    assert client.post("/api/ingest/chain", json=_chain(delayed=True)).status_code == 200
    ch = client.get("/api/symbol/DELL/chain").json()
    assert ch["delayed"] is True and ch["underlying"]["delayed"] is True
    assert client.post("/api/ingest/chain", json=_chain()).status_code == 200      # back to real-time
    assert client.get("/api/symbol/DELL/chain").json()["delayed"] is False
    assert client.get("/api/symbol/AAPL/chain").json()["delayed"] is False       # old data: unknown -> false
    nulls = _chain(contracts=[_contract(120.0, "C", bid=None, ask=None, volume=None, oi=None, iv=None,
                                        delta=None, gamma=None, theta=None, vega=None)],
                   underlying={"price": 120.0, "bid": None, "volume": None, "prev_close": None})
    r = client.post("/api/ingest/chain", json=nulls)
    assert r.status_code == 200, r.text
    # unknown volume / OI stays unknown end to end (stored NULL, served null), never 0
    from optionlens.analytics.chain import option_row
    from optionlens.repo import options
    (c,) = options.get_chain("DELL").contracts
    assert (c.volume, c.oi) == (None, None)
    assert {k: option_row(c, 120.0)[k] for k in ("volume", "oi")} == {"volume": None, "oi": None}
    client.post("/api/ingest/chain", json=_chain())


def test_chain_built_from_a_saved_contract_list_says_so(client):
    """The bridge built the chain from its saved contract list (IBKR's contract server unavailable): the chain says
    when that list was listed; a chain listed at fetch time (the default, older bridges) says nothing."""
    listed = "2026-11-18T15:00:00+00:00"
    assert client.post("/api/ingest/chain", json=_chain(contracts_from_cache=True, contracts_listed_at=listed)).status_code == 200
    ch = client.get("/api/symbol/DELL/chain").json()
    assert ch["contracts_from_cache"] is True
    assert datetime.fromisoformat(ch["contracts_listed_at"]) == datetime.fromisoformat(listed)
    assert client.post("/api/ingest/chain", json=_chain(contracts_from_cache=True)).status_code == 422
    assert client.post("/api/ingest/chain", json=_chain()).status_code == 200
    ch = client.get("/api/symbol/DELL/chain").json()
    assert ch["contracts_from_cache"] is False and ch["contracts_listed_at"] is None


def test_contracts_with_unknown_oi_or_volume_are_served_in_the_chain(client):
    """Weekend delayed-frozen data often has no OI / volume for whole expiries: those contracts (and so their
    expiries) must still be served, with null fields, never dropped by a SQL `oi >= 0` filter."""
    body = _chain(contracts=[_contract(120.0, "C"), _contract(120.0, "P"),
                             _contract(120.0, "C", expiry="2026-11-27", oi=None, volume=None),
                             _contract(120.0, "P", expiry="2026-11-27", oi=None, volume=5),
                             _contract(125.5, "C", expiry="2026-12-04", bid=None, ask=None, last=None, oi=None,
                                       volume=None, iv=None, delta=None)])
    assert client.post("/api/ingest/chain", json=body).status_code == 200
    from optionlens.services import context
    context.invalidate()                   # same fetched_at as the chain cached by earlier tests
    ch = client.get("/api/symbol/DELL/chain?expiry=2026-11-27").json()
    assert [e["expiry"] for e in ch["expiries"]] == ["2026-11-27", "2026-12-04", "2026-12-18"]
    assert [(r["oi"], r["volume"]) for r in ch["calls"] + ch["puts"]] == [(None, None), (None, 5)]
    no_quote = client.get("/api/symbol/DELL/chain?expiry=2026-12-04").json()["calls"]
    assert [(r["strike"], r["bid"], r["ask"], r["mid"], r["oi"]) for r in no_quote] == [(125.5, None, None, None, None)]
    client.post("/api/ingest/chain", json=_chain())
    context.invalidate()


def test_missing_iv_and_greeks_are_computed_and_labelled(client):
    """Weekend / delayed chains carry few model computations: the engine solves IV from the mid (else a last of the
    session) and derives greeks from it, each labelled; feed values keep the feed's label; theo is never computed."""
    body = _chain(contracts=[
        _contract(120.0, "C", gamma=0.02, theta=-0.05, vega=0.12),
        _contract(120.0, "P", iv=None, delta=None, bid=2.0, ask=2.2),
        _contract(125.5, "C", iv=None, delta=None, bid=None, ask=None, last=1.5, volume=7,
                  last_ts="2026-11-19T19:10:00+00:00"),
        _contract(115.0, "P", iv=None, delta=None, bid=None, ask=None, last=0.4, volume=None,
                  last_ts="2026-11-18T19:10:00+00:00")])                       # a trade of the day before: no IV
    assert client.post("/api/ingest/chain", json=body).status_code == 200
    from optionlens.services import context
    context.invalidate()
    ch = client.get("/api/symbol/DELL/chain").json()
    rows = {(r["strike"], r["right"]): r for r in ch["calls"] + ch["puts"]}
    feed, mid, last, stale = rows[(120.0, "C")], rows[(120.0, "P")], rows[(125.5, "C")], rows[(115.0, "P")]
    assert (feed["iv_source"], feed["greeks_source"], feed["iv"]) == ("ibkr", "ibkr", 0.3)
    assert mid["iv_source"] == "computed_mid" and 0.05 < mid["iv"] < 2 and mid["greeks_source"] == "computed"
    assert -1 < mid["delta"] < 0 and mid["theo"] is None
    assert last["iv_source"] == "computed_last" and last["last_ts"].startswith("2026-11-19T19:10")
    assert (stale["iv"], stale["iv_source"], stale["delta"], stale["greeks_source"]) == (None, None, None, None)
    assert ch["expiries"][0]["atm_iv_computed"] is False
    client.post("/api/ingest/chain", json=_chain())
    context.invalidate()


def test_a_weekend_snapshot_is_solved_at_the_session_close():
    """A Saturday chain holds Friday's closing quotes: time to expiry is measured from Friday 16:00 ET."""
    from optionlens.analytics import bs
    from optionlens.analytics.calendar import NY, year_fraction_to_expiry
    from optionlens.models import OptionContract, Quote
    from optionlens.services import ingest
    fri_close = datetime(2026, 11, 20, 16, 0, tzinfo=NY)
    saturday = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)
    t = year_fraction_to_expiry(date(2026, 11, 27), fri_close)
    q = ingest.ctx.dividend_yield("DELL", 120.0)
    price = bs.price(120.0, 120.0, t, ingest.ctx.risk_free_rate(date(2026, 11, 20)), q, 0.8, "C")
    c = OptionContract(osi="DELL261127C00120000", symbol="DELL", expiry=date(2026, 11, 27), strike=120.0, right="C",
                       bid=price - 0.01, ask=price + 0.01, last=None, change=None, volume=None, oi=None, iv=None,
                       delta=None, gamma=None, theta=None, vega=None, rho=None, theo=None)
    quote = Quote(symbol="DELL", ts=saturday, price=120.0, change=None, change_pct=None, open=None, high=None, low=None,
                  prev_close=None, volume=None, bid=None, ask=None, iv30=None)
    (out,) = ingest._with_computed_iv(quote, [c], saturday, "ibkr")
    assert out.iv_source == "computed_mid" and out.iv == pytest.approx(0.8, abs=0.01)


def test_ingest_final_chain_archives_the_session(client):
    close = datetime(2026, 11, 18, 21, 25, tzinfo=timezone.utc)   # 16:25 ET
    r = client.post("/api/ingest/chain", json=_chain(final=True, session_date="2026-11-18",
                                                     fetched_at=close.isoformat()))
    assert r.status_code == 200, r.text
    # restore the latest (non-final) DELL chain for other tests
    client.post("/api/ingest/chain", json=_chain())
    from optionlens.repo import options
    eod = options.eod_for_date(date(2026, 11, 18), ["DELL"])
    assert len(eod["osi"]) == 6 and float(eod["underlying"][0]) == 120.0


def test_ingest_final_chain_uses_the_regular_session_close(client):
    from optionlens.repo import metrics, options
    close = datetime(2026, 11, 16, 21, 25, tzinfo=timezone.utc)   # 16:25 ET: price 120 is an after-hours trade
    body = _chain(final=True, session_date="2026-11-16", fetched_at=close.isoformat(),
                  underlying={"price": 120.0, "prev_close": 118.0, "rth_close": 119.0})
    assert client.post("/api/ingest/chain", json=body).status_code == 200
    eod = options.eod_for_date(date(2026, 11, 16), ["DELL"])
    assert float(eod["underlying"][0]) == 119.0
    assert metrics.get_metrics("DELL", date(2026, 11, 16))["close"] == 119.0
    client.post("/api/ingest/chain", json=_chain())                               # restore the latest chain


@pytest.mark.parametrize("body,msg", [
    (_chain(final=True), "session_date is required"),
    (_chain(final=True, session_date="2026-11-19"), "is not after the close"),
    (_chain(fetched_at="2999-01-01T00:00:00+00:00"), "in the future"),
    (_chain(fetched_at="2026-11-19T14:30:00"), "fetched_at"),                          # naive time
    (_chain(contracts=[_contract(100, "X")]), "right"),
    (_chain(contracts=[_contract(-1, "C")]), "strike"),
    (_chain(symbol="bad symbol!"), "symbol"),
    (_chain(underlying={"price": 0}), "price"),
])
def test_ingest_chain_validation(client, body, msg):
    r = client.post("/api/ingest/chain", json=body)
    assert r.status_code == 422 and msg in r.json()["detail"], r.text


def test_ingest_chain_size_limit(client):
    from optionlens.api import routes_ingest
    assert routes_ingest.ChainIn.model_fields["contracts"].metadata[0].max_length == 50_000


def test_ingest_iv_history_feeds_volatility(client):
    days = [SESSION - timedelta(days=i) for i in range(299, -1, -1)]
    bars = [{"date": d.isoformat(), "value": 0.2 + i / 3000} for i, d in enumerate(days)]
    bars.append({"date": "2026-01-02", "value": -1})                                   # skipped: not positive
    r = client.post("/api/ingest/iv-history", json={"source": "ibkr", "symbol": "DELL", "kind": "iv30", "bars": bars})
    assert r.status_code == 200 and r.json() == {"stored": 300}
    v = client.get("/api/symbol/DELL/volatility").json()
    hist = {h["date"]: h["iv30"] for h in v["history"]}
    d = SESSION - timedelta(days=10)                         # SESSION itself has the chain's collected IV30
    assert hist[d.isoformat()] == pytest.approx(0.2 + 289 / 3000)
    assert v["iv_rank"] is not None and v["history_days"] == 252
    assert client.post("/api/ingest/iv-history", json={"source": "ibkr", "symbol": "DELL", "kind": "hv30",
                                                       "bars": []}).status_code == 422
    r = client.post("/api/ingest/hv-history", json={"source": "ibkr", "symbol": "DELL", "kind": "hv30",
                                                    "bars": bars[:3]})
    assert r.json() == {"stored": 3}


def test_ingest_index_bars_and_quotes(client):
    r = client.post("/api/ingest/index-bars", json={"source": "ibkr", "symbol": "^VIX", "bars": [
        {"date": "2026-11-18", "open": 20, "high": 22, "low": 19, "close": 21.5}]})
    assert r.json() == {"stored": 1}
    from optionlens.repo import market, quotes
    assert float(market.index_bars(["VIX"])["VIX"]["close"][-1]) == 21.5
    ts = "2026-11-19T20:00:00+00:00"
    q = {"source": "ibkr", "delayed": True, "quotes": [{"symbol": "VIX", "price": 27.8, "change": 3.26, "change_pct": 0.1327, "ts": ts}]}
    assert client.post("/api/ingest/quotes", json=q).json() == {"stored": 1}
    old = {"source": "ibkr", "delayed": False, "quotes": [{"symbol": "VIX", "price": 1.0, "ts": "2026-11-19T19:00:00+00:00"}]}
    assert client.post("/api/ingest/quotes", json=old).json() == {"stored": 0}        # older than stored
    assert quotes.get_quote("VIX").price == 27.8 and quotes.get_quote("VIX").delayed is True
    assert client.post("/api/ingest/quotes", json={"source": "ibkr", "quotes": []}).status_code == 422


def test_index_history_is_wanted_and_becomes_the_index_daily_bars(client):
    """Index pages (SPX, NDX, RUT, XSP) read the IBKR index history as their daily bars; the bridge is asked for
    10 years when it is missing and for the days after the last bar otherwise. The VIX family stays a market series."""
    from optionlens.analytics.calendar import last_completed_session, now_ny
    from optionlens.repo import market
    from optionlens.services import ingest
    today, last = now_ny().date(), last_completed_session(now_ny())
    want = {x["symbol"]: x["from"] for x in ingest.index_bars_want(today, last)}
    assert set(want) >= {"SPX", "NDX", "RUT", "XSP", "VIX", "VIX9D", "VIX3M", "VIX6M"}
    assert want["SPX"] == today - timedelta(days=3650)
    days = [last - timedelta(days=i) for i in range(3700) if (last - timedelta(days=i)).weekday() < 5][::-1]
    body = {"source": "ibkr", "symbol": "SPX", "bars": [
        {"date": d.isoformat(), "open": 4000 + i, "high": 4010 + i, "low": 3990 + i, "close": 4005 + i} for i, d in enumerate(days)]}
    assert client.post("/api/ingest/index-bars", json=body).json() == {"stored": len(days)}
    assert "SPX" not in market.index_coverage(["SPX"])                                   # not a market series
    want = {x["symbol"]: x["from"] for x in ingest.index_bars_want(today, last)}
    assert "SPX" not in want and "NDX" in want                                            # up to date now
    s = client.get("/api/symbol/SPX/summary").json()
    assert s["quote"]["price"] == pytest.approx(4005 + len(days) - 1) and s["quote"]["source"] == "ibkr"
    assert s["returns"]["y1"] is not None and s["high_52w"] is not None and s["hv20"] is not None
    pa = client.get("/api/symbol/SPX/price-action?range=1y").json()
    assert pa["rows"] and client.get("/api/symbol/SPX/bars?range=1m").json()


def test_ingest_wants(client):
    w = client.get("/api/ingest/wants").json()
    assert set(w) == {"iv_history", "hv_history", "history_symbols", "underlying_stats", "chain_universe",
                      "eod_chains", "option_bars", "index_symbols", "index_bars", "chains_on_demand_max_age_s"}
    assert w["history_symbols"] >= len(w["iv_history"]) and "AAPL" in {x["symbol"] for x in w["hv_history"]}
    fill = {"as_of_session"}                                # present outside the live hours (after-hours fill)
    assert set(w["chain_universe"]) - fill == {"symbols", "max_age_s", "expiries", "moneyness"}
    assert set(w["underlying_stats"]) - fill == {"symbols", "max_age_s"} and isinstance(w["option_bars"], list)
    assert w["chains_on_demand_max_age_s"] == 120 and set(w["index_symbols"]) == {"VIX", "VIX9D", "VIX3M", "VIX6M"}
    ivs = {x["symbol"]: x["from"] for x in w["iv_history"]}
    assert "AAPL" in ivs and ivs["AAPL"].endswith("-01-01")
    e = w["eod_chains"]
    assert {"session_date", "symbols", "not_before", "deadline"} <= set(e) and "SPX" in e["symbols"]


def test_history_wants_resume_from_the_last_backfilled_day_and_ignore_stats_points(client):
    """IV/HV history is wanted from 10 years back only without a backfilled series: a symbol whose history was
    ingested resumes the day after its last point; single-day IV30 values of underlying-stats pushes (tagged
    '<source>:stats', or legacy ones spanning a few days) do not count as a history."""
    from optionlens.repo import vol_history
    from optionlens.services import ingest
    last = date(2026, 11, 20)
    start = date(2016, 1, 1)
    vol_history.upsert("ZZH1", "iv30", "ibkr", [(date(2026, 1, 2) + timedelta(days=i), 0.3) for i in range(300)])
    vol_history.upsert("ZZH1", "iv30", "ibkr:stats", [(last, 0.31)])                          # stats: ignored
    vol_history.upsert("ZZH2", "iv30", "ibkr", [(date(2026, 11, 18), 0.9), (date(2026, 11, 19), 0.9)])  # legacy stats
    vol_history.upsert("ZZH2", "hv30", "ibkr", [(date(2016, 1, 4), 0.2), (last, 0.2)])          # current
    iv = {x["symbol"]: x["from"] for x in ingest.history_want("iv30", ["ZZH1", "ZZH2"], start, last)}
    hv = {x["symbol"]: x["from"] for x in ingest.history_want("hv30", ["ZZH1", "ZZH2"], start, last)}
    assert iv == {"ZZH1": date(2026, 1, 2) + timedelta(days=300), "ZZH2": start}
    assert hv == {"ZZH1": start}


def test_ingest_ack_wants_counts_accepted_option_bar_offers(client):
    body = {"kind": "option_bars", "items": [{"symbol": "aapl", "sessions": ["2026-11-17", "2026-11-18"]}]}
    r = client.post("/api/ingest/ack-wants", json=body)
    assert r.status_code == 200 and r.json() == {"recorded": 2}
    from optionlens.repo import options
    offers = options.catchup_offers([date(2026, 11, 17), date(2026, 11, 18)])
    assert {k: v[1] for k, v in offers.items()} == {(date(2026, 11, 17), "AAPL"): 1, (date(2026, 11, 18), "AAPL"): 1}
    assert client.post("/api/ingest/ack-wants", json={**body, "kind": "chains"}).status_code == 422
    assert client.post("/api/ingest/ack-wants", json={"kind": "option_bars", "items": [{"symbol": "AAPL", "sessions": []}]}).status_code == 422


def test_ingest_underlying_stats_fill_metrics_and_iv_history(client):
    body = {"source": "ibkr", "delayed": True, "session_date": "2026-11-17", "ts": "2026-11-17T20:30:00+00:00",
            "stats": [{"symbol": "zzzz", "price": 10.0, "call_volume": 100, "put_volume": 50, "iv30": 0.4},
                      {"symbol": "ZZZY", "price": 5.0, "call_volume": None, "call_oi": None, "iv30": None,
                       "hv30": None}]}
    r = client.post("/api/ingest/underlying-stats", json=body)
    assert r.status_code == 200 and r.json() == {"stored": 2}
    from optionlens.repo import metrics, vol_history
    m = metrics.get_metrics("ZZZZ", date(2026, 11, 17))
    assert m["call_volume"] == 100 and m["iv30"] == pytest.approx(0.4) and m["delayed"] is True
    assert vol_history.last_dates("iv30", ["ZZZZ"]) == {"ZZZZ": date(2026, 11, 17)}
    from optionlens.services import ingest                        # a stats value is not a backfilled history
    assert ingest.history_want("iv30", ["ZZZZ"], date(2016, 1, 1), date(2026, 11, 17)) == [
        {"symbol": "ZZZZ", "from": date(2016, 1, 1)}]
    bad = {**body, "stats": [{"symbol": "ZZZZ", "call_volume": -1}]}
    assert client.post("/api/ingest/underlying-stats", json=bad).status_code == 422


def test_ingest_option_bars_reconstruct_archive(client):
    bars = [{"expiry": "2026-12-18", "strike": k, "right": r, "date": "2026-11-17", "close": 3.0, "volume": 5}
            for k in (100.0, 105.0) for r in ("C", "P")]
    r = client.post("/api/ingest/option-bars", json={"source": "ibkr", "symbol": "DELL", "bars": bars})
    assert r.status_code == 200 and r.json()["stored"] == 4
    from optionlens.repo import options
    eod = options.eod_for_date(date(2026, 11, 17), ["DELL"])
    assert min(eod["osi"].tolist()) == "DELL261218C00100000"
    assert client.post("/api/ingest/option-bars", json={"source": "ibkr", "symbol": "DELL",
                                                        "bars": [{**bars[0], "close": None}]}).status_code == 422


def test_large_volume_contracts_without_greeks_show_unknown_not_zero(client):
    """A delayed chain without model computations nor a price to compute them from: IV, delta and delta volume are
    null, never 0."""
    body = _chain(delayed=True)
    for c in body["contracts"]:
        c.update(iv=None, delta=None, gamma=None, theta=None, vega=None, volume=5000, bid=None, ask=None, last=None)
    assert client.post("/api/ingest/chain", json=body).status_code == 200
    rows = [r for r in client.get("/api/reports/large-volume-contracts?min_volume=1000&symbols=DELL").json()["rows"]]
    assert rows and all(r["delta"] is None and r["iv"] is None and r["delta_volume"] is None for r in rows)
    body = _chain(delayed=True)
    for c in body["contracts"]:
        c.update(volume=5000)
    client.post("/api/ingest/chain", json=body)
    rows = client.get("/api/reports/large-volume-contracts?min_volume=1000&symbols=DELL").json()["rows"]
    assert all(r["delta_volume"] == pytest.approx(5000 * r["delta"] * 100) for r in rows)
    client.post("/api/ingest/chain", json=_chain())                     # leave DELL's chain as the other tests expect
