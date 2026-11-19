"""splits-calendar and new-listings: universe join, filters, month windows (offline; Nasdaq calendars faked)."""
from __future__ import annotations

from datetime import date

import pytest

from optionlens.services.reports import calendars

from .seed_reports import api, seeded  # noqa: F401 - fixtures


def test_splits_rows_types_and_universe_join(api):
    body = api.get("/api/reports/splits-calendar").json()
    by = {r["symbol"]: r for r in body["rows"]}
    assert by["AAPL"]["split_type"] == "Forward" and by["AAPL"]["in_universe"] is True
    assert by["AAPL"]["market_cap"] == 5.07e12
    assert by["YFOR"]["split_type"] == "Reverse" and by["YFOR"]["split_factor"] == pytest.approx(0.02)
    assert by["YFOR"]["in_universe"] is False and by["YFOR"]["price"] is None
    assert by["LARK"]["split_type"] == "Forward" and by["LARK"]["ratio"] == "5%"
    dates = [r["execution_date"] for r in body["rows"]]
    assert dates == sorted(dates)
    assert any("Nasdaq calendar; may be incomplete" in n for n in body["notes"])


def test_splits_only_universe(api):
    rows = api.get("/api/reports/splits-calendar?only_universe=true").json()["rows"]
    assert [r["symbol"] for r in rows] == ["AAPL"]


def test_split_type():
    assert [calendars.split_type(x) for x in (None, 1, 2, 0.5)] == [None, None, "Forward", "Reverse"]


def test_new_listings_status_filter_and_order(api):
    up = api.get("/api/reports/new-listings").json()
    assert up["params"] == {"status": "upcoming", "months": 2}
    assert {r["status"] for r in up["rows"]} == {"upcoming"} and len(up["rows"]) == 2 * 2   # one fixture per month
    assert [r["date"] for r in up["rows"]] == sorted(r["date"] for r in up["rows"])
    priced = api.get("/api/reports/new-listings?status=priced&months=1").json()["rows"]
    assert [r["symbol"] for r in priced] == ["VVVV", "MDRN"] or {r["symbol"] for r in priced} == {"MDRN", "VVVV"}
    assert [r["date"] for r in priced] == sorted((r["date"] for r in priced), reverse=True)
    filed = api.get("/api/reports/new-listings?status=filed").json()["rows"]
    assert {r["status"] for r in filed} == {"filed"} and any(r["symbol"] is None for r in filed)


def test_new_listings_param_validation(api):
    assert api.get("/api/reports/new-listings?status=withdrawn").status_code == 422
    assert api.get("/api/reports/new-listings?months=7").status_code == 422
    assert api.get("/api/reports/new-listings?months=0").status_code == 422


def test_month_windows():
    assert calendars._months(date(2026, 1, 15), range(-1, 1)) == ["2025-12", "2026-01"]
    assert calendars._months(date(2026, 11, 19), range(3)) == ["2026-11", "2026-12", "2027-01"]
