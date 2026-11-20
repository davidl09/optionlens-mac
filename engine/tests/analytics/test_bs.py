import math

import numpy as np
import pytest

from optionlens.analytics import bs


def test_hull_no_dividend_call_and_put():
    # Hull, Options Futures & Other Derivatives: S=42, K=40, r=10%, σ=20%, T=0.5
    assert bs.price(42, 40, 0.5, 0.10, 0.0, 0.2, "C") == pytest.approx(4.7594, abs=1e-4)
    assert bs.price(42, 40, 0.5, 0.10, 0.0, 0.2, "P") == pytest.approx(0.8086, abs=1e-4)


def test_hull_index_option_with_dividend_yield():
    # Hull: index 930, K 900, r 8%, q 3%, σ 20%, T = 2 months -> 51.83
    assert bs.price(930, 900, 2 / 12, 0.08, 0.03, 0.2, "C") == pytest.approx(51.83, abs=0.01)


def test_hull_greeks_example():
    # Hull greeks example: S=49, K=50, r=5%, σ=20%, T=20 weeks (0.3846)
    g = bs.greeks(49, 50, 0.3846, 0.05, 0.0, 0.2, "C")
    assert bs.price(49, 50, 0.3846, 0.05, 0.0, 0.2, "C") == pytest.approx(2.40, abs=0.005)
    assert g["delta"] == pytest.approx(0.522, abs=1e-3)
    assert g["gamma"] == pytest.approx(0.066, abs=1e-3)
    assert g["vega"] == pytest.approx(0.121, abs=1e-3)          # per 1 vol point
    assert g["theta"] == pytest.approx(-4.31 / 365, abs=1e-4)   # per calendar day
    assert g["rho"] == pytest.approx(0.0891, abs=1e-4)          # per 1% rate


@pytest.mark.parametrize("S,K,T,r,q,s", [(100, 90, 0.25, 0.04, 0.01, 0.3), (100, 120, 2.0, 0.05, 0.03, 0.15),
                                          (50, 50, 1 / 365, 0.0, 0.0, 0.8)])
def test_put_call_parity(S, K, T, r, q, s):
    c, p = bs.price(S, K, T, r, q, s, "C"), bs.price(S, K, T, r, q, s, "P")
    assert c - p == pytest.approx(S * math.exp(-q * T) - K * math.exp(-r * T), abs=1e-10)


@pytest.mark.parametrize("right", ["C", "P"])
@pytest.mark.parametrize("K", [80.0, 100.0, 125.0])
def test_greeks_match_finite_differences(right, K):
    S, T, r, q, s = 100.0, 0.4, 0.03, 0.015, 0.27
    g = bs.greeks(S, K, T, r, q, s, right)
    f = lambda **kw: bs.price(kw.get("S", S), K, kw.get("T", T), kw.get("r", r), q, kw.get("s", s), right)  # noqa
    h = 1e-3
    assert g["delta"] == pytest.approx((f(S=S + h) - f(S=S - h)) / (2 * h), abs=1e-6)
    assert g["gamma"] == pytest.approx((f(S=S + h) - 2 * f() + f(S=S - h)) / h**2, abs=1e-4)
    assert g["vega"] == pytest.approx((f(s=s + h) - f(s=s - h)) / (2 * h) / 100, abs=1e-6)
    assert g["rho"] == pytest.approx((f(r=r + h) - f(r=r - h)) / (2 * h) / 100, abs=1e-6)
    dt = 1e-5
    assert g["theta"] == pytest.approx(-(f(T=T + dt) - f(T=T - dt)) / (2 * dt) / 365, abs=1e-6)


def test_vectorized_matches_scalar():
    K = np.array([90.0, 100.0, 110.0])
    right = np.array(["C", "P", "C"])
    v = bs.price(100.0, K, 0.5, 0.02, 0.0, 0.25, right)
    for i in range(3):
        assert v[i] == pytest.approx(bs.price(100.0, K[i], 0.5, 0.02, 0.0, 0.25, right[i]))
    assert np.array_equal(bs.is_call(np.array([True, False])), [True, False])


@pytest.mark.parametrize("right", ["C", "P"])
@pytest.mark.parametrize("K", [60.0, 90.0, 100.0, 110.0, 150.0])
@pytest.mark.parametrize("T", [7 / 365, 0.25, 2.0])
@pytest.mark.parametrize("sigma", [0.08, 0.35, 1.5])
def test_iv_round_trip(right, K, T, sigma):
    S, r, q = 100.0, 0.04, 0.01
    p = bs.price(S, K, T, r, q, sigma, right)
    lo, _ = bs.no_arb_bounds(S, K, T, r, q, right)
    if p - lo < 1e-9 * S:  # numerically indistinguishable from intrinsic: IV not identifiable
        return
    iv = bs.implied_vol(p, S, K, T, r, q, right)
    assert iv is not None
    assert bs.price(S, K, T, r, q, iv, right) == pytest.approx(p, abs=1e-8)
    if p - lo > 1e-4:  # vega large enough to recover sigma itself
        assert iv == pytest.approx(sigma, rel=1e-4)


def test_iv_outside_no_arb_bounds_is_none():
    S, K, T, r, q = 100.0, 90.0, 0.5, 0.05, 0.0
    lo, hi = bs.no_arb_bounds(S, K, T, r, q, "C")
    assert bs.implied_vol(lo - 0.01, S, K, T, r, q, "C") is None   # below intrinsic of forward
    assert bs.implied_vol(hi + 0.01, S, K, T, r, q, "C") is None   # above spot
    assert bs.implied_vol(0.0, S, K, T, r, q, "C") is None
    assert bs.implied_vol(1.0, S, K, 0.0, r, q, "C") is None       # expired
    assert bs.implied_vol(float("nan"), S, K, T, r, q, "P") is None
    # attainable only with vol > 5 -> None
    assert bs.implied_vol(99.99, S, 100.0, 0.5, 0.0, 0.0, "C") is None


def test_expired_and_zero_vol_edge_cases():
    assert bs.price(105, 100, 0.0, 0.05, 0.0, 0.3, "C") == pytest.approx(5.0)
    assert bs.price(105, 100, -0.1, 0.05, 0.0, 0.3, "P") == pytest.approx(0.0)
    assert bs.price(95, 100, 0.0, 0.05, 0.0, 0.3, "P") == pytest.approx(5.0)
    g = bs.greeks(105, 100, 0.0, 0.05, 0.0, 0.3, "C")
    assert g["delta"] == 1.0 and g["gamma"] == 0.0 and g["vega"] == 0.0
    assert bs.greeks(105, 100, 0.0, 0.05, 0.0, 0.3, "P")["delta"] == 0.0
    # zero vol: discounted forward intrinsic
    assert bs.price(100, 90, 1.0, 0.05, 0.0, 0.0, "C") == pytest.approx(100 - 90 * math.exp(-0.05))
    # deep ITM long-dated call ≈ S e^{-qT} − K e^{-rT}
    assert bs.price(1000, 10, 1.0, 0.05, 0.02, 0.2, "C") == pytest.approx(
        1000 * math.exp(-0.02) - 10 * math.exp(-0.05), rel=1e-12)
