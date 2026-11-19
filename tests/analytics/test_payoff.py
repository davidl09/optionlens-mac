from datetime import date, datetime

import numpy as np
import pytest

from optionlens.analytics import bs, payoff, probability as prob
from optionlens.analytics.calendar import NY, year_fraction_to_expiry
from optionlens.analytics.strategies import Leg

NOW = datetime(2026, 1, 5, 16, 0, tzinfo=NY)
E1, E2 = date(2026, 2, 4), date(2026, 3, 6)
R, Q = 0.04, 0.0


def test_straddle_breakevens_and_unlimited_profit():
    legs = [Leg("option", 1, "C", 100, E1, 3.0, 0.3), Leg("option", 1, "P", 100, E1, 2.5, 0.3)]
    out = payoff.calculate(legs, 100.0, NOW, R, Q, hv30=0.25)
    assert out["cost"] == pytest.approx(550.0)
    assert out["breakevens"] == pytest.approx([94.5, 105.5], abs=1e-9)
    assert out["max_profit"] is None and out["max_loss"] == pytest.approx(550.0)
    assert out["expiry_used"] == E1
    T = year_fraction_to_expiry(E1, NOW)
    exp_pop = 1 - prob.prob_above(100, 94.5, T, 0.3, R) + prob.prob_above(100, 105.5, T, 0.3, R)
    assert out["pop"] == pytest.approx(exp_pop, abs=1e-5)
    ev = 100 * (prob.expected_call(100, 100, T, 0.3, R) + prob.expected_put(100, 100, T, 0.3, R)) - 550
    assert out["ev_iv"] == pytest.approx(ev, abs=0.05)
    assert len(out["prices"]) == 121 and len(out["pnl_expiry"]) == 121 and len(out["table"]) == 15
    # default range: ±max(20%, 3σ√T)
    half = max(0.20, 3 * 0.3 * np.sqrt(T))
    assert half > 0.2
    assert out["prices"][0] == pytest.approx(100 * (1 - half)) and out["prices"][-1] == pytest.approx(100 * (1 + half))
    short = payoff.calculate([Leg("option", 1, "C", 100, date(2026, 1, 9), 1.0, 0.2)], 100.0, NOW, R, Q)
    assert short["prices"][0] == pytest.approx(80.0) and short["prices"][-1] == pytest.approx(120.0)
    assert out["pnl_today"] == out["pnl_eval"]


def test_naked_short_call_unlimited_loss():
    legs = [Leg("option", -1, "C", 110, E1, 1.5, 0.3)]
    out = payoff.calculate(legs, 100.0, NOW, R, Q)
    assert out["max_loss"] is None and out["max_profit"] == pytest.approx(150.0)
    assert out["breakevens"] == pytest.approx([111.5])
    assert out["ev_hv"] is None  # no HV supplied
    assert out["greeks"]["delta"] == pytest.approx(-100 * bs.delta(100, 110, year_fraction_to_expiry(E1, NOW), R, Q, 0.3, "C"))


def test_vertical_extrema_exact_even_off_grid():
    legs = [Leg("option", 1, "C", 101.3, E1, 3.0, 0.3), Leg("option", -1, "C", 104.7, E1, 1.4, 0.3)]
    out = payoff.calculate(legs, 100.0, NOW, R, Q, points=7)
    assert out["max_profit"] == pytest.approx((3.4 - 1.6) * 100)
    assert out["max_loss"] == pytest.approx(160.0)
    assert out["breakevens"] == pytest.approx([102.9])


def test_calendar_expiry_curve_values_back_leg_with_bsm():
    legs = [Leg("option", -1, "C", 100, E1, 3.0, 0.30), Leg("option", 1, "C", 100, E2, 4.5, 0.28)]
    out = payoff.calculate(legs, 100.0, NOW, R, Q, iv_shift=0.02, price_range=(90, 110), points=5)
    assert out["expiry_used"] == E1
    tb = year_fraction_to_expiry(E2, datetime(2026, 2, 4, 16, 0, tzinfo=NY))
    at100 = 100 * bs.price(100, 100, tb, R, Q, 0.30, "C") - 150.0
    assert out["prices"][2] == 100 and out["pnl_expiry"][2] == pytest.approx(at100)
    assert out["max_loss"] == pytest.approx(150.0, rel=0.02)   # both legs → 0 or parity far away
    assert out["max_profit"] == pytest.approx(at100, rel=1e-6)
    assert len(out["breakevens"]) == 2


def test_stock_and_eval_date():
    legs = [Leg("stock", 100, price=100.0), Leg("option", -1, "C", 105, E1, 2.0, 0.3)]
    out = payoff.calculate(legs, 100.0, NOW, R, Q, eval_date=date(2026, 1, 20), price_range=(80, 120), points=41)
    assert out["max_profit"] == pytest.approx(700.0) and out["max_loss"] == pytest.approx(9800.0, rel=1e-6)
    assert out["breakevens"] == pytest.approx([98.0])
    at = datetime(2026, 1, 20, 16, 0, tzinfo=NY)
    i = out["prices"].index(100.0)
    exp = 100 * 100 - 100 * bs.price(100, 105, year_fraction_to_expiry(E1, at), R, Q, 0.3, "C") - (10000 - 200)
    assert out["pnl_eval"][i] == pytest.approx(exp)
    assert out["greeks"]["delta"] < 100
    assert out["legs"][0]["kind"] == "stock" and out["legs"][1]["price"] == 2.0
