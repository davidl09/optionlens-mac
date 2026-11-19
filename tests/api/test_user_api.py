"""Watchlists, presets, alerts and notifications over HTTP (validation, 404/422, partial PUTs)."""
from __future__ import annotations

from optionlens.repo import user as user_repo

from .seed_reports import api, seeded  # noqa: F401 - fixtures


def test_watchlist_crud(api):
    r = api.post("/api/watchlists", json={"name": "Tech", "symbols": ["aapl", "XYZ", "AAPL"]})
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["name"] == "Tech" and w["symbols"] == ["AAPL", "XYZ"]
    wid = w["id"]
    assert api.post("/api/watchlists", json={"name": "Tech"}).status_code == 422       # duplicate name
    assert api.post("/api/watchlists", json={"name": "  "}).status_code == 422
    assert api.get(f"/api/watchlists/{wid}").json()["symbols"] == ["AAPL", "XYZ"]
    r = api.put(f"/api/watchlists/{wid}", json={"name": "Tech 2"}).json()
    assert r["name"] == "Tech 2" and r["symbols"] == ["AAPL", "XYZ"]                     # partial: symbols kept
    r = api.put(f"/api/watchlists/{wid}", json={"symbols": ["SPY"]}).json()
    assert r["name"] == "Tech 2" and r["symbols"] == ["SPY"]
    assert api.post(f"/api/watchlists/{wid}/symbols/aapl").json()["symbols"] == ["SPY", "AAPL"]
    assert api.delete(f"/api/watchlists/{wid}/symbols/SPY").json()["symbols"] == ["AAPL"]
    assert api.post(f"/api/watchlists/{wid}/symbols/NOPE1").status_code == 404          # unknown symbol (offline)
    t = api.get(f"/api/watchlists/{wid}/table").json()
    assert [r["symbol"] for r in t["rows"]] == ["AAPL"]
    row = t["rows"][0]
    assert {"symbol", "name", "price", "change_pct", "iv30", "iv_rank", "next_earnings"} <= set(row)
    assert row["price"] > 0 and row["iv30"] > 0 and row["next_earnings"]
    assert api.delete(f"/api/watchlists/{wid}").json() == {"ok": True}
    assert api.get(f"/api/watchlists/{wid}").status_code == 404
    assert api.delete(f"/api/watchlists/{wid}").status_code == 404
    assert api.get("/api/watchlists/999/table").status_code == 404


def test_presets(api):
    body = {"report_id": "naked-puts", "name": "safe", "payload": {"params": {"delta_max": 0.2},
                                                                   "filters": {"pop": {"min": 0.7}}}}
    p = api.post("/api/presets", json=body).json()
    assert p["report_id"] == "naked-puts" and p["payload"]["filters"]["pop"]["min"] == 0.7
    body["payload"]["params"]["delta_max"] = 0.25
    p2 = api.post("/api/presets", json=body).json()                                     # same name: overwrite
    assert p2["id"] == p["id"] and p2["payload"]["params"]["delta_max"] == 0.25
    assert [x["name"] for x in api.get("/api/presets?report_id=naked-puts").json()] == ["safe"]
    assert api.post("/api/presets", json={**body, "report_id": "nope"}).status_code == 422
    assert api.post("/api/presets", json={**body, "payload": {"params": 3}}).status_code == 422
    assert api.delete(f"/api/presets/{p['id']}").json() == {"ok": True}
    assert api.delete(f"/api/presets/{p['id']}").status_code == 404


def test_alerts_crud_and_validation(api, seeded):
    a = api.post("/api/alerts", json={"kind": "iv30_above", "symbol": "aapl", "params": {"value": 0.3},
                                      "enabled": True, "cooldown_min": 30}).json()
    assert a["symbol"] == "AAPL" and a["params"] == {"value": 0.3} and a["cooldown_min"] == 30
    assert a["last_fired_at"] is None
    bad = [{"kind": "nope", "symbol": "AAPL", "params": {"value": 1}, "enabled": True, "cooldown_min": 1},
           {"kind": "price_above", "symbol": "AAPL", "params": {}, "enabled": True, "cooldown_min": 1},
           {"kind": "price_above", "symbol": None, "params": {"value": 1}, "enabled": True, "cooldown_min": 1},
           {"kind": "price_above", "symbol": None, "params": {"value": 1, "watchlist_id": 999}, "enabled": True,
            "cooldown_min": 1},
           {"kind": "price_above", "symbol": "AAPL", "params": {"value": 1}, "enabled": True, "cooldown_min": -5}]
    for b in bad:
        assert api.post("/api/alerts", json=b).status_code == 422, b
    w = api.post("/api/alerts", json={"kind": "unusual_volume", "symbol": None,
                                      "params": {"watchlist_id": seeded["watchlist_id"]}, "enabled": True,
                                      "cooldown_min": 240}).json()
    assert w["symbol"] is None and w["params"] == {"watchlist_id": seeded["watchlist_id"]}
    u = api.put(f"/api/alerts/{a['id']}", json={"enabled": False}).json()
    assert u["enabled"] is False and u["kind"] == "iv30_above" and u["params"] == {"value": 0.3}
    u = api.put(f"/api/alerts/{a['id']}", json={"params": {"value": 0.4}, "cooldown_min": 5}).json()
    assert u["params"] == {"value": 0.4} and u["cooldown_min"] == 5 and u["symbol"] == "AAPL"
    assert api.put(f"/api/alerts/{a['id']}", json={"symbol": None}).status_code == 422  # no scope left
    assert api.put("/api/alerts/999", json={"enabled": False}).status_code == 404
    for x in (a, w):
        assert api.delete(f"/api/alerts/{x['id']}").json() == {"ok": True}
    assert api.delete(f"/api/alerts/{a['id']}").status_code == 404


def test_notifications(api):
    ids = [user_repo.add_notification(f"t{i}", "b", "/s/AAPL") for i in range(3)]
    unread = api.get("/api/notifications?unread=1").json()
    assert {n["id"] for n in unread} >= set(ids) and all(not n["read"] for n in unread)
    r = api.post("/api/notifications/read", json={"ids": ids[:1]}).json()
    assert r["updated"] == 1
    assert ids[0] not in {n["id"] for n in api.get("/api/notifications?unread=1").json()}
    r = api.post("/api/notifications/read").json()                                     # no body: all
    assert r["unread"] == 0
    assert api.get("/api/notifications?unread=1").json() == []
    assert len(api.get("/api/notifications").json()) >= 3
