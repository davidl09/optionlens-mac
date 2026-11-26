"""/api/portfolio and POST /api/ingest/positions (docs/INTEGRATION.md §11): Questrade's accounts (fetched, read-only),
the missing account permission as its own state, IBKR's pushed positions, masking, FX, settings, live prices, the risk
graph, the TS contract, and AI agents seeing the portfolio only when the user allowed it."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from optionlens import live
from optionlens.api.app import create_app
from optionlens.config import get_settings
from optionlens.mcp import catalog
from optionlens.mcp.connection import Engine, EngineError
from optionlens.mcp.server import OptionLensTools
from optionlens.services import mcp_access, portfolio
from optionlens.sources import chain_source
from optionlens.sources.questrade.errors import ACCOUNT_ACCESS
from optionlens.sources.questrade.source import QuestradeSource

from ..questrade_fake import FakeQuestrade
from .tsschema import load_types, mismatches

OSI = "AAPL261218C00350000"
IBKR_ACCOUNT = "U1234567"
QT_ACCOUNT = "26598145"


def ibkr_body(**kw) -> dict:
    return {"source": "ibkr", "fetched_at": datetime.now(timezone.utc).isoformat(),
            "positions": [
                {"account": IBKR_ACCOUNT, "sec_type": "STK", "symbol": "AAPL", "currency": "USD", "qty": 100,
                 "avg_cost": 300.0, "con_id": 265598},
                {"account": IBKR_ACCOUNT, "sec_type": "OPT", "symbol": "AAPL", "currency": "USD", "qty": -1,
                 "avg_cost": 850.0, "multiplier": 100, "expiry": "2026-12-18", "strike": 350, "right": "C",
                 "trading_class": "AAPL"},
                {"account": IBKR_ACCOUNT, "sec_type": "STK", "symbol": "ZERO", "currency": "USD", "qty": 0,
                 "avg_cost": 1.0}],
            "accounts": [{"account": IBKR_ACCOUNT, "currency": "USD", "net_liquidation": 50_000, "cash": 10_000,
                          "buying_power": 80_000}],
            "fx": [{"currency": "CAD", "base": "USD", "rate": 0.72}], **kw}


@pytest.fixture(autouse=True)
def fresh(client):
    portfolio.reset()
    path = get_settings().data_dir / "portfolio.json"
    path.unlink(missing_ok=True)
    yield
    portfolio.reset()
    path.unlink(missing_ok=True)


@pytest.fixture
def qt(client, monkeypatch, tmp_path):
    fake = FakeQuestrade()
    src = QuestradeSource(tmp_path, 8, 0.25, fake.http)
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: src)
    assert client.post("/api/sources/questrade/token", json={"refresh_token": "pasted-token"}).status_code == 200
    return fake, src


def find(p: dict, symbol: str) -> dict:
    return next(r for u in p["underlyings"] for r in u["positions"] if r["symbol"] == symbol)


# ----------------------------------------------------------------------------- IBKR
def test_ibkr_positions_ingest_masking_and_contract(client):
    r = client.post("/api/ingest/positions", json=ibkr_body())
    assert r.status_code == 200 and r.json() == {"stored": 2, "accounts": 1, "ignored": False}
    assert IBKR_ACCOUNT not in r.text
    p = client.get("/api/portfolio").json()
    text = client.get("/api/portfolio").text
    assert IBKR_ACCOUNT not in text and "1234567" not in text
    src = {s["broker"]: s for s in p["sources"]}
    assert src["ibkr"]["state"] == "ok" and src["ibkr"]["positions"] == 2
    opt = find(p, OSI)
    assert opt["account"] == "•••567" and opt["avg_cost"] == pytest.approx(8.5)    # IBKR's per-contract cost / 100
    assert opt["price_source"] == "stored" and opt["price"] > 0 and opt["iv"] and opt["delta"] < 0
    stock = find(p, "AAPL")
    assert stock["cost_basis"] == 30_000 and stock["market_value"] == pytest.approx(stock["price"] * 100)
    und = next(u for u in p["underlyings"] if u["underlying"] == "AAPL")
    assert und["risk"] is True and und["totals"]["by_currency"]["USD"]["delta"] == pytest.approx(100 + opt["delta"])
    assert p["balance_totals"]["by_currency"]["USD"]["net_liquidation"] == 50_000
    assert mismatches(p, load_types().parse("Portfolio")) == []


def test_ibkr_canadian_positions_are_valued_with_the_toronto_listing(client):
    """IBKR names a Canadian contract by ticker + CAD (+ the venue for a stock); OptionLens values it with the
    Canadian listing's own data, in CAD, under the Montréal Exchange code every chain source uses."""
    from optionlens.repo import symbols
    symbols.upsert_symbols([{"symbol": s, "kind": "stock"} for s in ("RY.TO", "ABC.VN")])
    now = datetime.now(timezone.utc).isoformat()
    quotes = [{"symbol": "RY.TO", "price": 121.0, "prev_close": 120.0, "ts": now},
              {"symbol": "RY", "price": 86.0, "ts": now}]                # the NYSE listing: another price, in USD
    assert client.post("/api/ingest/quotes", json={"source": "ibkr", "delayed": True, "quotes": quotes}).status_code == 200
    pos = {"account": IBKR_ACCOUNT, "currency": "CAD", "qty": 100, "avg_cost": 100.0}
    body = ibkr_body(positions=[
        {**pos, "sec_type": "STK", "symbol": "RY", "exchange": "TSE"},
        {**pos, "sec_type": "OPT", "symbol": "RY", "exchange": "CDE", "qty": -1, "avg_cost": 250.0, "multiplier": 100,
         "expiry": "2026-12-18", "strike": 120, "right": "C", "trading_class": "RY"},
        {**pos, "sec_type": "STK", "symbol": "ABC"},                      # no venue named: the listing OptionLens knows
        {**pos, "sec_type": "STK", "symbol": "NOWHERE"},                  # no known Canadian listing: never guessed
        {**pos, "sec_type": "STK", "symbol": "BRK B", "currency": "USD"}])
    assert client.post("/api/ingest/positions", json=body).json()["stored"] == 5
    p = client.get("/api/portfolio").json()
    ry = find(p, "RY.TO")
    assert ry["currency"] == "CAD" and ry["price"] == 121.0 and ry["price_source"] == "stored"   # not RY's 86 USD
    assert ry["market_value"] == 12_100 and ry["unrealized_pnl"] == 2_100 and ry["day_pnl"] == 100
    assert ry["linkable"] is True and ry["beta"] is None                 # beta-weighting is vs SPY: USD only
    opt = find(p, "RY.TO261218C00120000")
    assert opt["underlying"] == "RY.TO" and opt["avg_cost"] == pytest.approx(2.5) and opt["spot"] == 121.0
    assert find(p, "ABC.VN")["underlying"] == "ABC.VN"
    lost = find(p, "NOWHERE")
    assert lost["currency"] == "CAD" and lost["price"] is None and lost["linkable"] is False
    assert find(p, "BRK.B")["currency"] == "USD"
    assert p["totals"]["by_currency"]["CAD"]["market_value"] == 12_100   # CAD on its own, never summed into USD
    assert "RY.TO" in p["live"]["symbols"] and "RY.TO261218C00120000" in p["live"]["contracts"]
    assert not [u for u in p["underlyings"] if u["underlying"] == "RY"]
    assert mismatches(p, load_types().parse("Portfolio")) == []


def test_ibkr_fx_converts_totals_with_a_labelled_rate(client):
    client.post("/api/ingest/positions", json=ibkr_body())
    usd = client.get("/api/portfolio").json()
    cad = client.get("/api/portfolio?currency=CAD").json()
    mv = usd["totals"]["converted"]["values"]["market_value"]
    assert cad["totals"]["converted"]["values"]["market_value"] == pytest.approx(mv / 0.72)
    assert cad["totals"]["converted"]["rates"][0]["source"] == "IBKR account summary (ExchangeRate)"
    assert client.get("/api/portfolio?currency=EUR").status_code == 422


@pytest.mark.parametrize("bad", [{"positions": [{"account": "U1", "sec_type": "STK", "symbol": "A", "qty": "x"}]},
                                 {"source": "questrade"},
                                 {"positions": [{"account": "U 1; drop", "sec_type": "STK", "symbol": "A", "qty": 1}]}])
def test_ingest_validation(client, bad):
    r = client.post("/api/ingest/positions", json={**ibkr_body(), **bad})
    assert r.status_code == 422 and IBKR_ACCOUNT not in r.text


def test_switching_a_broker_off_hides_and_stops_it(client):
    client.post("/api/ingest/positions", json=ibkr_body())
    assert client.get("/api/ingest/live-wants").json()["positions"] is True
    assert client.get("/api/portfolio/settings").json() == {"questrade": True, "ibkr": True}
    assert client.put("/api/portfolio/settings", json={"ibkr": False}).json() == {"questrade": True, "ibkr": False}
    assert client.get("/api/ingest/live-wants").json()["positions"] is False       # the bridge stops its feed
    p = client.get("/api/portfolio").json()
    assert p["underlyings"] == [] and {s["broker"]: s["state"] for s in p["sources"]}["ibkr"] == "disabled"
    assert client.post("/api/ingest/positions", json=ibkr_body()).json()["ignored"] is True
    assert client.get("/api/portfolio").json()["underlyings"] == []


def test_live_values_reprice_positions(client, monkeypatch):
    client.post("/api/ingest/positions", json=ibkr_body())
    hub = live.instance()
    hub.publish("questrade", {"AAPL": {"price": 400.0, "prev_close": 390.0}}, {OSI: {"bid": 50.0, "ask": 52.0, "iv": 0.25}},
                persist=False)
    try:
        p = client.get("/api/portfolio").json()
        stock, opt = find(p, "AAPL"), find(p, OSI)
        assert (stock["price"], stock["price_source"], stock["market_value"]) == (400.0, "live", 40_000.0)
        assert stock["day_pnl"] == pytest.approx(1_000.0) and stock["day_pnl_basis"] == "prev_close"
        assert (opt["price"], opt["price_source"], opt["iv"], opt["spot"]) == (51.0, "live", 0.25, 400.0)
        assert opt["unrealized_pnl"] == pytest.approx(-(51.0 - 8.5) * 100)
        assert OSI in p["live"]["contracts"] and {"AAPL", "SPY"} <= set(p["live"]["symbols"])
    finally:
        hub._latest_q.pop("AAPL", None)
        hub._latest_o.pop(OSI, None)


def test_risk_graph(client):
    client.post("/api/ingest/positions", json=ibkr_body())
    r = client.get("/api/portfolio/risk?underlying=aapl")
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["underlying"] == "AAPL" and len(g["legs"]) == 2 and g["skipped"] == []
    assert g["cost"] == pytest.approx(100 * 300.0 - 1 * 100 * 8.5)                # each leg at its cost basis
    assert len(g["pnl_expiry"]) == len(g["pnl_today"]) == len(g["prices"]) and g["expiry_used"] == "2026-12-18"
    assert client.get("/api/portfolio/risk?underlying=MSFT").status_code == 404


def test_nothing_held(client):
    p = client.get("/api/portfolio").json()
    assert p["underlyings"] == [] and p["totals"]["positions"] == 0
    states = {s["broker"]: s for s in p["sources"]}
    assert states["questrade"]["state"] == "not_connected" and states["ibkr"]["state"] == "waiting"


# ----------------------------------------------------------------------------- Questrade
def test_questrade_positions(client, qt):
    fake, _ = qt
    p = client.get("/api/portfolio").json()
    text = client.get("/api/portfolio").text
    assert QT_ACCOUNT not in text
    src = {s["broker"]: s for s in p["sources"]}["questrade"]
    assert src["state"] == "ok" and src["positions"] == 3
    call = find(p, "XYZ261218C00110000")
    assert call["account"] == "Margin •••145" and call["qty"] == -2 and call["avg_cost"] == 2.1
    thi = find(p, "THI.TO")
    assert thi["currency"] == "CAD" and thi["price_source"] == "broker" and thi["linkable"] is True   # a TSX listing
    usd = p["totals"]["by_currency"]
    assert set(usd) == {"CAD", "USD"}                                     # never summed across currencies
    assert p["totals"]["converted"]["rates"][0]["source"].startswith("Questrade balances")
    before = fake.path_count("/v1/accounts")
    client.get("/api/portfolio")                                          # within 55 s: no new account call
    assert fake.path_count("/v1/accounts") == before
    client.get("/api/portfolio?refresh=true")                             # on demand: fetched again
    assert fake.path_count("/v1/accounts") == before + 1
    assert all(r.method == "GET" for r in fake.requests if r.url.path.startswith("/v1/accounts"))


def test_questrade_missing_account_permission(client, qt, monkeypatch):
    fake, src = qt
    fake.account_scope_denied = True
    p = client.get("/api/portfolio?refresh=true").json()
    q = {s["broker"]: s for s in p["sources"]}["questrade"]
    assert q["state"] == "needs_permission" and q["detail"] == ACCOUNT_ACCESS
    st = client.get("/api/sources/questrade").json()
    assert st["state"] == "connected" and st["detail"] is None            # market data is not affected
    assert st["account_access"] == {"state": "needs_permission", "detail": ACCOUNT_ACCESS}
    n = fake.path_count("/v1/accounts")
    client.get("/api/portfolio")                                          # not asked again until on demand / new token
    assert fake.path_count("/v1/accounts") == n
    fake.account_scope_denied = False
    fake.valid.add("new-token")
    body = client.post("/api/sources/questrade/token", json={"refresh_token": "new-token"}).json()
    assert body["account_access"]["state"] == "ok"


def test_questrade_off_makes_no_account_call(client, qt):
    fake, _ = qt
    client.put("/api/portfolio/settings", json={"questrade": False})
    n = fake.path_count("/v1/accounts")
    p = client.get("/api/portfolio?refresh=true").json()
    assert fake.path_count("/v1/accounts") == n and p["underlyings"] == []


# ----------------------------------------------------------------------------- AI agents
TOKEN = "app-token-0123456789abcdef"
LOCAL = ("127.0.0.1", 50123)


@pytest.fixture
def agent(client):
    app = create_app(replace(get_settings(), api_token=TOKEN))
    c = TestClient(app, client=LOCAL)
    c.put("/api/mcp", json={"enabled": True}, headers={"Authorization": f"Bearer {TOKEN}"})
    yield app, c, {"Authorization": f"Bearer {mcp_access.instance().token()}"}
    mcp_access.instance().disable()


def test_agents_see_the_portfolio_only_when_allowed(client, agent):
    app, c, key = agent
    app_auth = {"Authorization": f"Bearer {TOKEN}"}
    client.post("/api/ingest/positions", json=ibkr_body())
    r = c.get("/api/portfolio", headers=key)
    assert r.status_code == 403 and r.json()["code"] == "mcp_forbidden" and "see my portfolio" in r.json()["detail"]
    assert c.get("/api/portfolio/risk?underlying=AAPL", headers=key).status_code == 403
    assert c.get("/api/mcp", headers=app_auth).json()["portfolio"] is False
    assert c.put("/api/mcp", json={"portfolio": True}, headers=app_auth).json()["portfolio"] is True
    r = c.get("/api/portfolio", headers=key)
    assert r.status_code == 200 and IBKR_ACCOUNT not in r.text
    assert c.get("/api/portfolio/settings", headers=key).status_code == 403          # settings: the app's only
    assert c.put("/api/portfolio/settings", json={"ibkr": False}, headers=key).status_code == 403
    assert c.post("/api/ingest/positions", json=ibkr_body(), headers=key).status_code == 403
    c.put("/api/mcp", json={"enabled": False}, headers=app_auth)                     # off resets the permission
    c.put("/api/mcp", json={"enabled": True}, headers=app_auth)
    assert c.get("/api/mcp", headers=app_auth).json()["portfolio"] is False
    assert c.put("/api/mcp", json={"enabled": False, "portfolio": True}, headers=app_auth).json()["portfolio"] is False
    assert c.put("/api/mcp", json={"portfolio": True}, headers=app_auth).status_code == 409   # access off


async def test_mcp_server_hides_portfolio_tools_unless_allowed(client, agent):
    app, _, _ = agent
    client.post("/api/ingest/positions", json=ibkr_body())
    access = mcp_access.instance()
    access.listening(8473)
    engine = Engine(get_settings().data_dir, transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)))
    tools = OptionLensTools(engine, catalog.build(app.openapi()))
    try:
        names = {t.name for t in tools.listed()}
        assert "portfolio_positions" not in names and "portfolio_risk" not in names and "symbol_summary" in names
        with pytest.raises(EngineError) as e:
            await tools.call("portfolio_positions", {})
        assert "Allow agents to see my portfolio" in str(e.value)
        access.set_portfolio(True)
        assert {"portfolio_positions", "portfolio_risk"} <= {t.name for t in tools.listed()}
        got = await tools.call("portfolio_positions", {})
        assert got["underlyings"][0]["underlying"] == "AAPL"
        assert "1234567" not in str(got)
    finally:
        await engine.aclose()
