"""Portfolio valuation and aggregation (pure): position math, greeks, beta weighting, FX rules (never a silent
cross-currency sum), and the risk graph's composition from holdings at their cost basis."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from optionlens.analytics.strategies import Leg, position_greeks
from optionlens.portfolio import risk, valuation as v
from optionlens.portfolio.model import FxRate, Holding

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 11, 24, 14, 0, tzinfo=timezone.utc)


def stock(sym="XYZ", qty=100, cost=90.0, cur="USD", broker="questrade", **kw) -> Holding:
    return Holding(broker, "a1", "Margin •••145", "stock", sym, sym, cur, qty, cost, **kw)


def option(qty=-2, cost=2.0, strike=110.0, right="C", expiry=date(2026, 12, 18), mult=100.0, **kw) -> Holding:
    osi = f"XYZ261218{right}{int(strike * 1000):08d}"
    return Holding("questrade", "a1", "Margin •••145", "option", osi, "XYZ", "USD", qty, cost, mult, osi, expiry,
                   right, strike, **kw)


def test_stock_position_math():
    r = v.position(stock(), v.Mark(100.0, "live", NOW, prev_close=98.0), now=NOW, r=0.04, q=0.0, beta=1.5,
                   spy_price=500.0)
    assert (r["market_value"], r["cost_basis"], r["unrealized_pnl"]) == (10_000.0, 9_000.0, 1_000.0)
    assert r["unrealized_pct"] == pytest.approx(1 / 9)
    assert (r["day_pnl"], r["day_pnl_basis"]) == (200.0, "prev_close")
    assert (r["delta"], r["delta_dollars"], r["gamma"], r["theta"]) == (100.0, 10_000.0, 0.0, 0.0)
    assert r["bwd_delta_spy"] == pytest.approx(100 * 100 * 1.5 / 500)           # 30 SPY shares


def test_broker_day_pnl_wins_and_short_option_math():
    h = option(broker_day_pnl=-15.0)
    mk = v.Mark(1.25, "broker", NOW, prev_close=1.4, spot=100.0, iv=0.3)
    r = v.position(h, mk, now=NOW, r=0.04, q=0.0)
    assert (r["market_value"], r["cost_basis"]) == (-250.0, -400.0)
    assert r["unrealized_pnl"] == 150.0 and r["unrealized_pct"] == pytest.approx(150 / 400)
    assert (r["day_pnl"], r["day_pnl_basis"]) == (-15.0, "broker")
    g = position_greeks([Leg("option", -2, "C", 110.0, date(2026, 12, 18), iv=0.3)], 100.0, NOW, 0.04, 0.0)
    assert r["delta"] == pytest.approx(g["delta"]) and r["theta"] == pytest.approx(g["theta"])
    assert r["delta"] < 0 and r["theta"] > 0 and r["vega"] < 0                   # short call
    assert r["delta_dollars"] == pytest.approx(g["delta"] * 100.0)


def test_unknowns_are_none_not_zero():
    r = v.position(option(), v.Mark(None, None, None, spot=None, iv=None), now=NOW, r=0.04, q=0.0)
    for k in ("price", "market_value", "unrealized_pnl", "day_pnl", "delta", "theta", "bwd_delta_spy"):
        assert r[k] is None, k
    assert r["cost_basis"] == -400.0


def test_non_standard_multiplier_scales_greeks():
    a = v.position(option(qty=1, mult=100.0), v.Mark(3.0, "stored", NOW, spot=100.0, iv=0.3), now=NOW, r=0.0, q=0.0)
    b = v.position(option(qty=1, mult=10.0), v.Mark(3.0, "stored", NOW, spot=100.0, iv=0.3), now=NOW, r=0.0, q=0.0)
    assert b["delta"] == pytest.approx(a["delta"] / 10) and b["market_value"] == pytest.approx(30.0)


USDCAD = FxRate("USD", "CAD", 1.40, "Questrade balances", NOW)


def test_fx_rate_direct_inverse_hop_and_missing():
    assert v.fx_rate("USD", "USD", [])[0] == 1.0
    assert v.fx_rate("USD", "CAD", [USDCAD])[0] == pytest.approx(1.40)
    assert v.fx_rate("CAD", "USD", [USDCAD])[0] == pytest.approx(1 / 1.40)
    eur = FxRate("EUR", "CAD", 1.5, "IBKR", NOW)
    assert v.fx_rate("EUR", "USD", [USDCAD, eur])[0] == pytest.approx(1.5 / 1.4)
    assert v.fx_rate("EUR", "USD", [USDCAD]) is None
    older = FxRate("USD", "CAD", 1.30, "old", T0)
    assert v.fx_rate("USD", "CAD", [older, USDCAD])[0] == pytest.approx(1.40)   # the freshest wins


def rows():
    us = v.position(stock(), v.Mark(100.0, "live", NOW), now=NOW, r=0.0, q=0.0)
    ca = v.position(stock("THI.TO", 100, 60.0, "CAD"), v.Mark(70.0, "broker", NOW), now=NOW, r=0.0, q=0.0)
    return [us, ca]


def test_aggregate_keeps_currencies_apart_and_converts_only_with_a_rate():
    agg = v.aggregate(rows(), "USD", [USDCAD])
    assert agg["by_currency"]["USD"]["market_value"] == 10_000 and agg["by_currency"]["CAD"]["market_value"] == 7_000
    conv = agg["converted"]
    assert conv["values"]["market_value"] == pytest.approx(10_000 + 7_000 / 1.4)
    assert conv["missing_fx"] == [] and conv["rates"][0]["label"].startswith("1 USD = 1.4000 CAD")
    none = v.aggregate(rows(), "USD", [])
    assert none["converted"]["values"] is None and none["converted"]["missing_fx"] == ["CAD"]
    assert none["by_currency"]["USD"]["market_value"] == 10_000       # per-currency sums stay


def test_unknown_values_are_counted():
    rs = rows() + [v.position(stock("ABC"), v.Mark(None), now=NOW, r=0.0, q=0.0)]
    agg = v.aggregate(rs, "USD", [USDCAD])
    assert agg["by_currency"]["USD"]["unknown"]["market_value"] == 1 and agg["by_currency"]["USD"]["market_value"] == 10_000


def test_beta_weighted_counts_what_it_could_not_weight():
    r1 = v.position(stock(), v.Mark(100.0), now=NOW, r=0.0, q=0.0, beta=1.2, spy_price=600.0)
    r2 = v.position(stock("THI.TO", cur="CAD"), v.Mark(70.0), now=NOW, r=0.0, q=0.0, beta=1.0, spy_price=600.0)
    bw = v.beta_weighted([r1, r2], 600.0)
    assert bw["spy_shares"] == pytest.approx(100 * 100 * 1.2 / 600) and bw["excluded"] == 1
    assert bw["dollars"] == pytest.approx(bw["spy_shares"] * 600)


def test_risk_graph_composes_every_leg_at_its_cost_basis():
    hs = [stock(qty=100, cost=90.0), option(qty=-1, cost=2.0, strike=110.0)]
    out = risk.graph(hs, {hs[1].osi: 0.3}, 100.0, NOW, 0.04, 0.0)
    assert out["cost"] == pytest.approx(100 * 90.0 - 1 * 100 * 2.0)            # entry prices = average costs
    assert [l["kind"] for l in out["legs"]] == ["stock", "option"] and out["skipped"] == []
    assert out["expiry_used"] == date(2026, 12, 18)
    above = out["pnl_expiry"][-1]                                              # far above the strike: capped
    assert above == pytest.approx((110 - 90) * 100 + 200, rel=1e-6)
    assert len(out["pnl_today"]) == len(out["prices"]) == 121


def test_risk_graph_skips_legs_without_iv_or_cost():
    hs = [option(qty=1, cost=None), option(qty=1, strike=120.0)]
    out = risk.graph(hs, {}, 100.0, NOW, 0.04, 0.0)
    assert out["legs"] == [] and {s["reason"] for s in out["skipped"]} == {"no average cost", "no IV for this contract"}
