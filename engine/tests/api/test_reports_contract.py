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
                                                      "truncated", "notes", "method", "empty_reason",
                                                      "extra", "source"}


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


def test_protective_put_is_out_of_the_money_by_at_least_the_target(api):
    """DELL-style regression: a 5% OTM target must never pick an in-the-money put (strike above spot)."""
    for otm in (0.05, 0.1):
        rows = api.get(f"/api/reports/put-protection?otm_pct={otm}&target_dte=30").json()["rows"]
        assert rows
        for r in rows:
            assert r["strike"] <= r["price"] * (1 - otm) + 1e-6
            assert r["otm_pct"] == pytest.approx(1 - r["strike"] / r["price"]) and r["otm_pct"] >= otm - 1e-9


def test_todays_earnings_opportunities_on_a_weekend_say_which_session(api, monkeypatch):
    from datetime import date
    from optionlens.services.reports import earnings
    monkeypatch.setattr(earnings, "today", lambda: date(2026, 11, 21))      # Saturday
    notes = api.get("/api/reports/earnings-todays-opportunities").json()["notes"]
    assert any("Markets are closed today (Saturday)" in n and "2026-11-23" in n for n in notes)
