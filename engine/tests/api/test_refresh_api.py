"""POST/GET /api/symbol/{sym}/refresh and POST /api/ingest/refresh-result (docs/INTEGRATION.md §7)."""
from __future__ import annotations

from optionlens.sources import chain_source

from .tsschema import load_types, mismatches


class Idle:
    """A connected server-side source that is never asked in these tests (its round attempt fails as unusable)."""
    name = "questrade"

    def connected(self):
        return True

    def unavailable(self):
        return "waiting to retry the login"

    def problem(self):
        return None


def states(st):
    return {s["name"]: s["state"] for s in st["sources"]}


def test_refresh_api(client, monkeypatch):
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", Idle)
    schema = load_types().parse("RefreshStatus")
    r = client.post("/api/symbol/AAPL/refresh")
    assert r.status_code == 200 and r.json()["symbol"] == "AAPL" and mismatches(r.json(), schema) == []
    assert states(r.json()) == {"questrade": "failed"}
    r = client.get("/api/symbol/AAPL/refresh")
    assert r.status_code == 200 and mismatches(r.json(), schema) == []
    r = client.post("/api/ingest/refresh-result", json={"source": "ibkr", "symbol": "AAPL", "status": "failed",
                                                        "message": "TWS error 200: No security definition"})
    assert r.status_code == 200 and states(r.json())["ibkr"] == "failed" and mismatches(r.json(), schema) == []
    assert client.post("/api/ingest/refresh-result", json={"source": "ibkr", "symbol": "AAPL",
                                                           "status": "bogus"}).status_code == 422
    assert client.get("/api/symbol/MSFT/refresh").json()["sources"] == []
