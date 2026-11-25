"""POST /api/calc/strategy through an earnings event: the IV crush (earnings_crush), the event block, EV(HV30) and
per-expiry vega. The CRM calendar runs on a synthetic chain at a fixed time; the AAPL tests on the seeded fixture."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import numpy as np
import pytest

from optionlens.analytics import bs
from optionlens.analytics.calendar import NY, year_fraction_to_expiry
from optionlens.analytics.chain import ChainArrays
from optionlens.services import calc as calc_service

NOW = datetime(2026, 11, 25, 16, 0, tzinfo=NY)
EXPIRIES = {date(2026, 11, 27): 0.55, date(2026, 12, 4): 0.917, date(2026, 12, 24): 0.62}
SPOT, R = 194.0, 0.04
CALENDAR = [{"kind": "option", "right": "C", "strike": 200, "expiry": "2026-12-04", "qty": -1, "price": 9.0},
            {"kind": "option", "right": "C", "strike": 200, "expiry": "2026-12-24", "qty": 1, "price": 11.0}]


def crm_chain() -> ChainArrays:
    """CRM ~194, earnings 2026-12-02 after the close (reaction 2026-12-03); flat IV per expiry."""
    cols = {k: [] for k in ("expiry", "strike", "right", "bid", "ask", "iv")}
    for e, iv in EXPIRIES.items():
        for k in range(170, 225, 5):
            for right in "CP":
                p = float(bs.price(SPOT, k, year_fraction_to_expiry(e, NOW), R, 0.0, iv, right))
                for key, v in zip(cols, (e, float(k), right, max(p - 0.05, 0.01), p + 0.05, iv)):
                    cols[key].append(v)
    n = len(cols["strike"])
    none = [None] * n
    return ChainArrays.from_columns(**cols, last=none, volume=none, oi=none, delta=none, gamma=none, theta=none,
                                    vega=none, spot=SPOT, asof=NOW, symbol="CRM", r=R, q=0.0, hv30=0.35,
                                    next_earnings=date(2026, 12, 2), next_reaction=date(2026, 12, 3))


@pytest.fixture
def crm(monkeypatch):
    box = {"ca": crm_chain()}
    monkeypatch.setattr(calc_service.ctx, "load_chain_arrays", lambda sym, ensure_fresh=True: box["ca"])
    monkeypatch.setattr(calc_service, "now_ny", lambda: NOW)
    return box


def post(client, legs, **extra):
    r = client.post("/api/calc/strategy", json={"symbol": "CRM", "legs": legs, "points": 201, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_calendar_through_earnings_is_valued_after_the_crush(client, crm):
    on, off = post(client, CALENDAR), post(client, CALENDAR, earnings_crush=False)
    assert on["cost"] == off["cost"] == pytest.approx(200.0)
    ev = on["event"]
    assert ev["applied"] is True and ev["reason"] is None and ev["method"] == "post_event_pair"
    assert ev["earnings_date"] == "2026-12-02" and ev["reaction_date"] == "2026-12-03"
    assert ev["expiries_used"] == ["2026-12-04", "2026-12-24"]
    assert ev["implied_move_pct"] == pytest.approx(0.128, abs=1e-3) and ev["base_iv"] == pytest.approx(0.423, abs=1e-3)
    assert off["event"]["applied"] is False and "earnings_crush=false" in off["event"]["reason"]
    assert off["event"]["implied_move_pct"] == ev["implied_move_pct"]
    # the Dec-24 leg at Dec 4 is worth its ex-event value: max profit ~$590, not ~$979
    assert on["max_profit"] == pytest.approx(590, abs=25)
    assert off["max_profit"] == pytest.approx(979, abs=25)
    assert on["pop"] < off["pop"] and on["ev_iv"] < off["ev_iv"]
    assert on["breakevens"][0] > off["breakevens"][0] and on["breakevens"][1] < off["breakevens"][1]
    front, back = on["legs"]
    assert back["iv_expiry"] == pytest.approx(0.423, abs=1e-3) and back["iv_eval"] == pytest.approx(0.62)
    assert back["iv_base"] == pytest.approx(0.423, abs=1e-3) and back["base_floored"] is False
    assert front["iv_expiry"] is None and front["iv_eval"] == pytest.approx(0.917)
    assert off["legs"][1]["iv_expiry"] == pytest.approx(0.62) and off["legs"][1]["iv_base"] is None
    # today's P/L and greeks do not depend on the crush (every leg is at its own IV now)
    assert np.allclose(on["pnl_today"], off["pnl_today"]) and on["greeks"] == pytest.approx(off["greeks"])
    assert on["pnl_eval"] == on["pnl_today"]


def test_iv_shift_adds_vol_points_to_the_crushed_iv(client, crm):
    r = post(client, CALENDAR, iv_shift=0.05)
    back = r["legs"][1]
    assert back["iv_eval"] == pytest.approx(0.67) and back["iv_expiry"] == pytest.approx(back["iv_base"] + 0.05)


def test_eval_date_before_and_after_the_event(client, crm):
    before = post(client, CALENDAR, eval_date="2026-12-02")["legs"][1]["iv_eval"]
    after = post(client, CALENDAR, eval_date="2026-12-03")["legs"][1]["iv_eval"]
    assert before > 0.62 and after == pytest.approx(0.423, abs=1e-3)


def test_ev_hv_is_withheld_when_earnings_fall_before_expiry(client, crm):
    for r in (post(client, CALENDAR), post(client, CALENDAR, earnings_crush=False)):
        assert r["ev_hv"] is None and "2026-12-03" in r["ev_hv_reason"] and "HV30" in r["ev_hv_reason"]
    early = post(client, [{"kind": "option", "right": "P", "strike": 190, "expiry": "2026-11-27", "qty": 1}])
    assert early["event"] is None and early["ev_hv"] is not None and early["ev_hv_reason"] is None


def test_vega_by_expiry_sums_to_net_vega(client, crm):
    legs = CALENDAR + [{"kind": "option", "right": "P", "strike": 190, "expiry": "2026-12-24", "qty": 2},
                       {"kind": "stock", "qty": 100}]
    r = post(client, legs)
    buckets = {b["expiry"]: b["vega"] for b in r["vega_by_expiry"]}
    assert list(buckets) == ["2026-12-04", "2026-12-24"]
    assert sum(buckets.values()) == pytest.approx(r["greeks"]["vega"])
    assert buckets["2026-12-04"] == pytest.approx(r["legs"][0]["greeks"]["vega"]) and buckets["2026-12-04"] < 0
    assert buckets["2026-12-24"] == pytest.approx(r["legs"][1]["greeks"]["vega"] + r["legs"][2]["greeks"]["vega"])
    assert r["legs"][3]["greeks"]["vega"] == 0 and r["legs"][3]["iv_eval"] is None


def test_no_earnings_or_no_estimate(client, crm):
    crm["ca"] = replace(crm_chain(), next_earnings=None, next_reaction=None)
    plain = post(client, CALENDAR)
    assert plain["event"] is None and plain["ev_hv"] is not None
    assert plain["max_profit"] == pytest.approx(post(client, CALENDAR, earnings_crush=False)["max_profit"], abs=25)
    # a flat term structure prices no earnings premium: the event is reported, not applied
    flat = crm_chain()
    crm["ca"] = replace(flat, iv=np.full(len(flat), 0.5))
    r = post(client, [{**l, "iv": 0.5} for l in CALENDAR])
    assert r["event"]["applied"] is False and "no earnings premium" in r["event"]["reason"]
    assert r["legs"][1]["iv_expiry"] == pytest.approx(0.5)


def test_aapl_fixture_event_through_the_route(client):
    """The seeded AAPL earnings (2027-01-28 after the close) react 2027-01-29: inside a Feb expiry's life."""
    feb = {"kind": "option", "right": "C", "strike": 270, "expiry": "2027-02-19", "qty": 1}
    r = client.post("/api/calc/strategy", json={"symbol": "AAPL", "legs": [feb]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["event"]["reaction_date"] == "2027-01-29" and out["event"]["earnings_date"] == "2027-01-28"
    assert isinstance(out["event"]["applied"], bool)
    assert out["ev_hv"] is None and "2027-01-29" in out["ev_hv_reason"]
    dec = client.post("/api/calc/strategy", json={"symbol": "AAPL", "legs": [{**feb, "expiry": "2026-12-18"}]}).json()
    assert dec["event"] is None and dec["ev_hv"] is not None
