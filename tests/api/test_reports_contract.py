"""Contract: report / market / user-data JSON keys equal the web/src/api/types.ts interfaces
(ReportDef, ReportResult, MarketOverview, Watchlist, Preset, Alert, Notification)."""
from __future__ import annotations

import pytest

from .seed_reports import api, seeded  # noqa: F401 - fixtures
from .tsschema import load_types, mismatches

TYPES = load_types()


def check(value, iface: str) -> None:
    assert mismatches(value, TYPES.parse(iface)) == []


def test_types_ts_has_the_interfaces():
    for name in ("ReportDef", "ParamDef", "ColumnDef", "ReportResult", "MarketOverview", "Watchlist", "Preset",
                 "PresetPayload", "Alert", "Notification"):
        assert name in TYPES.raw
    assert TYPES.interface_fields("ReportResult") == {"id", "title", "asof", "params", "columns", "rows",
                                                      "truncated", "notes", "extra", "source"}


def test_report_defs(api):
    body = api.get("/api/reports").json()
    check(body, "ReportDef[]")
    for d in body:
        check(d["params"], "ParamDef[]")
        check(d["columns"], "ColumnDef[]")


@pytest.mark.parametrize("url", ["/api/reports/unusual-option-volume", "/api/reports/options-broad-view",
                                 "/api/reports/covered-calls", "/api/reports/earnings-calendar",
                                 "/api/reports/stock-screener", "/api/compare/straddle?symbols=AAPL,XYZ&dte=30"])
def test_report_results(api, url):
    r = api.get(url)
    assert r.status_code == 200, r.text
    check(r.json(), "ReportResult")
    check(r.json()["columns"], "ColumnDef[]")


def test_market_overview(api):
    r = api.get("/api/market/overview")
    assert r.status_code == 200, r.text
    m = r.json()
    check(m, "MarketOverview")
    assert [c["symbol"] for c in m["indices"]][:4] == ["SPY", "QQQ", "IWM", "DIA"]
    assert {v["tenor"] for v in m["vix_term"]} == {"VIX9D", "VIX", "VIX3M", "VIX6M"}
    assert m["vix_history"] and m["watchlist"] and m["unusual_options"]


def test_user_data_shapes(api, seeded):
    check(api.get("/api/watchlists").json(), "Watchlist[]")
    check(api.get(f"/api/watchlists/{seeded['watchlist_id']}/table").json(), "ReportResult")
    p = api.post("/api/presets", json={"report_id": "covered-calls", "name": "contract",
                                       "payload": {"params": {"dte_min": 10}, "filters": {}, "sort": None, "hidden": []}})
    assert p.status_code == 200, p.text
    check(p.json(), "Preset")
    check(p.json()["payload"], "PresetPayload")
    a = api.post("/api/alerts", json={"kind": "price_above", "symbol": "AAPL", "params": {"value": 1.0},
                                      "enabled": False, "cooldown_min": 60})
    assert a.status_code == 200, a.text
    check(a.json(), "Alert")
    check(api.get("/api/alerts").json(), "Alert[]")
    check(api.get("/api/notifications").json(), "Notification[]")
