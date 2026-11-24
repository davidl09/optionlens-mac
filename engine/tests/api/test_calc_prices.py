"""POST /api/calc/strategy uses the prices, IVs and spot the renderer sends (the calculator's effective prices:
the user's pinned price, else the live mid, else the stored mid) and never re-derives them server-side."""
from __future__ import annotations

import numpy as np
import pytest

EXP = "2026-12-18"
CALL = {"kind": "option", "right": "C", "strike": 350, "expiry": EXP}


def calc(client, legs, **extra):
    r = client.post("/api/calc/strategy", json={"symbol": "AAPL", "legs": legs, "price_range": [280, 420], **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_provided_option_price_drives_cost_pnl_and_ev(client):
    a = calc(client, [{**CALL, "qty": 2, "price": 5.0, "iv": 0.3}])
    b = calc(client, [{**CALL, "qty": 2, "price": 7.5, "iv": 0.3}])
    d_cost = 2 * 100 * 2.5
    assert a["legs"][0]["price"] == 5.0 and b["legs"][0]["price"] == 7.5
    assert b["cost"] - a["cost"] == pytest.approx(d_cost)
    for key in ("pnl_expiry", "pnl_eval", "pnl_today"):
        assert np.allclose(np.array(b[key]) - np.array(a[key]), -d_cost)
    assert b["ev_iv"] - a["ev_iv"] == pytest.approx(-d_cost, abs=1e-3)
    assert b["ev_hv"] - a["ev_hv"] == pytest.approx(-d_cost, abs=1e-3)
    assert b["pop"] <= a["pop"]                                      # a dearer entry is less often profitable
    assert b["breakevens"][0] - a["breakevens"][0] == pytest.approx(2.5, abs=0.01)
    assert b["max_loss"] == pytest.approx(2 * 100 * 7.5)


def test_provided_iv_is_used_not_the_contract_iv(client):
    lo = calc(client, [{**CALL, "qty": 1, "price": 5.0, "iv": 0.2}])
    hi = calc(client, [{**CALL, "qty": 1, "price": 5.0, "iv": 0.6}])
    assert lo["legs"][0]["iv"] == 0.2 and hi["legs"][0]["iv"] == 0.6
    assert hi["greeks"]["vega"] != pytest.approx(lo["greeks"]["vega"])
    assert hi["ev_iv"] != pytest.approx(lo["ev_iv"])
    assert lo["cost"] == hi["cost"] == pytest.approx(500)           # the price stays the provided one


def test_provided_spot_and_stock_price(client):
    stock = [{"kind": "stock", "qty": 100, "price": 300.0}]
    r = calc(client, stock, spot=330.0)
    assert r["spot"] == 330.0 and r["cost"] == pytest.approx(30_000) and r["legs"][0]["price"] == 300.0
    i = int(np.argmin(np.abs(np.array(r["prices"]) - 330.0)))
    assert r["pnl_today"][i] == pytest.approx((r["prices"][i] - 300.0) * 100)
    assert r["greeks"]["delta"] == pytest.approx(100)
    # without a stock price the (live) spot is the entry price
    r2 = calc(client, [{"kind": "stock", "qty": 100}], spot=330.0)
    assert r2["legs"][0]["price"] == 330.0 and r2["cost"] == pytest.approx(33_000)


def test_spot_moves_probabilities_not_prices(client):
    legs = [{**CALL, "qty": 1, "price": 5.0, "iv": 0.3}]
    lo, hi = calc(client, legs, spot=320.0), calc(client, legs, spot=380.0)
    assert lo["cost"] == hi["cost"] == pytest.approx(500)
    assert hi["pop"] > lo["pop"] and hi["ev_iv"] > lo["ev_iv"] and hi["greeks"]["delta"] > lo["greeks"]["delta"]
