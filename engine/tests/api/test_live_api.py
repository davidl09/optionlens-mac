"""/api/live (docs/INTEGRATION.md §9): subscribe (validation, chain views resolved against the served chain), the SSE
stream under the Bearer token, the bridge's live-wants, and the calculator's live spot."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from optionlens import live
from optionlens.api.app import create_app
from optionlens.config import get_settings

TOKEN = "t" * 32
CID = "window-test-1"


@pytest.fixture(scope="module")
def authed(client):
    return TestClient(create_app(replace(get_settings(), api_token=TOKEN)))


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_subscribe_resolves_chain_views_and_replaces_the_set(client):
    chain = client.get("/api/symbol/AAPL/chain?expiry=2026-12-18").json()
    r = client.post("/api/live/subscribe", json={"client_id": CID, "symbols": ["spy"], "chains": [
        {"symbol": "AAPL", "expiry": "2026-12-18", "strike_min": 340, "strike_max": 360}]})
    assert r.status_code == 200, r.text
    want = [c["osi"] for c in chain["calls"] + chain["puts"] if 340 <= c["strike"] <= 360]
    body = r.json()
    assert body["symbols"] == 2 and body["contracts"] == len(want) > 0
    w = live.instance().registry.get(CID)
    assert w.symbols == {"AAPL", "SPY"} and set(w.contracts) == set(want) and set(w.contracts.values()) == {"AAPL"}
    osi = want[0]
    r = client.post("/api/live/subscribe", json={"client_id": CID, "option_contracts": [osi, "SPXW261218C06000000"]})
    w = live.instance().registry.get(CID)
    assert w.symbols == frozenset() and w.contracts == {osi: "AAPL", "SPXW261218C06000000": "SPX"}
    client.post("/api/live/subscribe", json={"client_id": CID})                   # empty: unsubscribed
    assert live.instance().registry.get(CID).empty


@pytest.mark.parametrize("body", [{"client_id": "x"}, {"client_id": CID, "symbols": ["BAD SYMBOL"]},
                                  {"client_id": CID, "symbols": ["A"] * 501}, {"client_id": CID, "extra": 1}])
def test_subscribe_validation(client, body):
    assert client.post("/api/live/subscribe", json=body).status_code == 422


def test_stream_needs_the_bearer_token_and_is_event_stream(authed, monkeypatch):
    assert authed.get(f"/api/live/stream?client_id={CID}").status_code == 401
    assert authed.post("/api/live/subscribe", json={"client_id": CID}).status_code == 401
    assert authed.get("/api/live/stream?client_id=x", headers=_auth()).status_code == 422

    async def one(client_id):
        yield f"event: status\ndata: {{\"client\":\"{client_id}\"}}\n\n"
    monkeypatch.setattr(live.instance(), "events", one)
    r = authed.get(f"/api/live/stream?client_id={CID}", headers=_auth())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.text == f'event: status\ndata: {{"client":"{CID}"}}\n\n'


def test_status_and_bridge_live_wants(client):
    assert "state" in client.get("/api/live/status").json()
    w = client.get("/api/ingest/live-wants").json()
    assert w["symbols"] == [] and w["interval_s"] > 0                            # Questrade is not the fallback


def test_calc_takes_the_live_spot(client):
    legs = [{"kind": "stock", "qty": 100}]
    base = client.post("/api/calc/strategy", json={"symbol": "AAPL", "legs": legs}).json()
    live_ = client.post("/api/calc/strategy", json={"symbol": "AAPL", "legs": legs, "spot": base["spot"] + 5}).json()
    assert live_["spot"] == pytest.approx(base["spot"] + 5) and live_["legs"][0]["price"] == pytest.approx(base["spot"] + 5)
    assert client.post("/api/calc/strategy", json={"symbol": "AAPL", "legs": legs, "spot": -1}).status_code == 422


def test_calc_legs_name_their_contract(client):
    chain = client.get("/api/symbol/AAPL/chain?expiry=2026-12-18").json()
    row = next(c for c in chain["calls"] if c["strike"] == 350)
    r = client.post("/api/calc/strategy", json={"symbol": "AAPL", "legs": [
        {"kind": "option", "right": "C", "strike": 350, "expiry": "2026-12-18", "qty": 1}, {"kind": "stock", "qty": 100}]}).json()
    assert r["legs"][0]["osi"] == row["osi"] and r["legs"][1]["osi"] is None
