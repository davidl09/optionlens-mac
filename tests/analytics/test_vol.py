import math
from datetime import date, datetime

import numpy as np
import pytest

from optionlens.analytics import bs, vol
from optionlens.analytics.calendar import NY
from optionlens.analytics.chain import ChainArrays

from .helpers import contract

NOW = datetime(2026, 1, 5, 16, 0, tzinfo=NY)  # Monday close


def test_hv_hand_computed():
    closes = [100, 101, 99, 102, 103]
    r = np.diff(np.log(closes))
    exp = np.std(r[-3:], ddof=1) * math.sqrt(252)
    assert vol.hv(closes, 3) == pytest.approx(exp)
    assert vol.hv(closes, 4) == pytest.approx(np.std(r, ddof=1) * math.sqrt(252))
    assert vol.hv(closes, 5) is None  # needs 6 closes
    s = vol.hv_series(closes, 3)
    assert np.isnan(s[:3]).all() and s[3] == pytest.approx(vol.hv(closes[:4], 3)) and s[4] == pytest.approx(exp)


def test_hv_on_gbm_recovers_sigma():
    rng = np.random.default_rng(7)
    sigma = 0.30
    r = rng.normal(-0.5 * sigma**2 / 252, sigma / math.sqrt(252), 5000)
    closes = 100 * np.exp(np.concatenate([[0], np.cumsum(r)]))
    snap = vol.hv_snapshot(closes)
    assert set(snap) == {"hv10", "hv20", "hv30", "hv60", "hv90", "hv252"}
    # stderr of sample stdev ≈ σ/√(2n): 252 obs -> ~1.3 vol pts; use 4 stderr
    assert snap["hv252"] == pytest.approx(sigma, abs=4 * sigma / math.sqrt(2 * 252))
    assert vol.hv(closes, 4999) == pytest.approx(sigma, abs=4 * sigma / math.sqrt(2 * 4999))
    assert vol.hv_snapshot(closes[:15])["hv20"] is None


def test_parkinson_constant_range():
    h = np.full(30, 101.0)
    l = np.full(30, 99.0)
    exp = math.sqrt(math.log(101 / 99) ** 2 / (4 * math.log(2))) * math.sqrt(252)
    assert vol.parkinson(h, l, 20) == pytest.approx(exp)
    assert vol.parkinson(h[:5], l[:5], 20) is None


def test_realized_forward_alignment():
    closes = np.exp(np.cumsum(np.r_[0, np.random.default_rng(1).normal(0, 0.01, 60)]))
    rf = vol.realized_forward(closes, 21)
    assert rf[0] == pytest.approx(vol.hv(closes[:22], 21))
    assert rf[10] == pytest.approx(vol.hv(closes[10:32], 21))
    assert np.isnan(rf[len(closes) - 21:]).all() and np.isfinite(rf[len(closes) - 22])


def test_iv_rank_percentile_rules():
    short = [0.2] * 19
    out = vol.iv_rank_percentile(short)
    assert out["iv_rank"] is None and out["iv_percentile"] is None and out["history_days"] == 19
    hist = [i / 100 for i in range(10, 31)] + [0.20]
    out = vol.iv_rank_percentile(hist)
    assert out["history_days"] == 22
    assert out["iv_rank"] == pytest.approx(0.5)
    assert out["iv_percentile"] == pytest.approx(10 / 22)  # strictly below 0.20: 0.10..0.19 (10 values)
    flat = vol.iv_rank_percentile([0.25] * 30)
    assert flat["iv_rank"] is None and flat["iv_percentile"] == 0.0
    # only the last 252 values count; None values are dropped
    long = [0.9] + [None] + list(np.linspace(0.1, 0.2, 252))
    o = vol.iv_rank_percentile(long)
    assert o["history_days"] == 252 and o["iv_high_52w"] == pytest.approx(0.2) and o["iv_rank"] == pytest.approx(1.0)


def test_total_variance_interpolation_exact():
    t = np.array([30 / 365, 90 / 365])
    iv = np.array([0.20, 0.30])
    target = 60 / 365
    w = 0.2**2 * t[0] + (0.3**2 * t[1] - 0.2**2 * t[0]) * (target - t[0]) / (t[1] - t[0])
    assert vol.interp_total_variance(t, iv, target) == pytest.approx(math.sqrt(w / target))
    assert vol.interp_total_variance(t, iv, 10 / 365) == 0.20     # flat extrapolation
    assert vol.interp_total_variance(t, iv, 400 / 365) == 0.30
    assert vol.interp_total_variance(t, iv, t[1]) == pytest.approx(0.30)
    rows = [{"t": 0.5 / 365, "dte": 0, "atm_iv": 0.9},  # DTE < 1 ignored
            {"t": t[0], "dte": 30, "atm_iv": 0.2}, {"t": t[1], "dte": 90, "atm_iv": 0.3}]
    cm = vol.constant_maturity(rows)
    assert cm[30] == pytest.approx(0.2) and cm[360] == 0.3 and cm[60] == pytest.approx(math.sqrt(w / target))


def test_forward_vol():
    assert vol.forward_vol(0.2, 0.1, 0.3, 0.2) == pytest.approx(math.sqrt((0.09 * 0.2 - 0.04 * 0.1) / 0.1))
    assert vol.forward_vol(0.5, 0.1, 0.2, 0.2) is None
    assert vol.forward_vol(0.2, 0.2, 0.3, 0.2) is None


def _smile_chain(spot=100.0, expiries=(date(2026, 1, 30), date(2026, 2, 20)), slope=-0.4):
    """Synthetic chain with a linear-in-moneyness smile (puts richer) and BSM feed deltas."""
    rows = []
    for e in expiries:
        T = (e - NOW.date()).days / 365
        for k in np.arange(70, 131, 2.5):
            iv = 0.25 + slope * (k / spot - 1.0)
            for right in "CP":
                d = bs.delta(spot, k, T, 0.0, 0.0, iv, right)
                p = bs.price(spot, k, T, 0.0, 0.0, iv, right)
                rows.append(contract(e, k, right, max(p - 0.05, 0.01), p + 0.05, iv=iv, delta=d, volume=10, oi=100))
    return ChainArrays.from_contracts(rows, spot, NOW, symbol="TEST")


def test_atm_iv_interpolates_in_strike():
    ca = _smile_chain(spot=101.0)
    atm = vol.atm_iv_by_expiry(ca)
    # strikes 100 and 102.5 bracket 101: IV linear in strike -> exact smile value
    k0, k1 = 100.0, 102.5
    iv0, iv1 = 0.25 - 0.4 * (k0 / 101 - 1), 0.25 - 0.4 * (k1 / 101 - 1)
    assert atm[0]["atm_iv"] == pytest.approx(iv0 + (101 - k0) / (k1 - k0) * (iv1 - iv0))
    assert atm[0]["dte"] == 25


def test_atm_iv_one_sided_and_missing():
    e = date(2026, 2, 20)
    rows = [contract(e, 90, "C", 10, 11, iv=0.3), contract(e, 90, "P", 0.1, 0.2, iv=0.4),
            contract(e, 110, "C", 0.1, 0.2), contract(e, 110, "P", 10, 11)]  # 110 has no IV
    ca = ChainArrays.from_contracts(rows, 100.0, NOW)
    assert vol.atm_iv_by_expiry(ca)[0]["atm_iv"] == pytest.approx(0.35)
    ca2 = ChainArrays.from_contracts(rows[2:], 100.0, NOW)
    assert vol.atm_iv_by_expiry(ca2)[0]["atm_iv"] is None


def test_skew_sign_on_synthetic_smile():
    ca = _smile_chain()
    rows = vol.delta_skew_by_expiry(ca)
    assert all(r["put25"] > r["call25"] for r in rows)
    term = vol.atm_iv_by_expiry(ca)
    atm30 = vol.constant_maturity(term)[30]
    sk = vol.skew_30(ca, atm30)
    assert sk["skew25_30"] > 0 and sk["put_skew_30"] > 0
    flat = _smile_chain(slope=0.0)
    sk0 = vol.skew_30(flat, 0.25)
    assert sk0["skew25_30"] == pytest.approx(0.0, abs=1e-12) and sk0["put_skew_30"] == pytest.approx(0.0, abs=1e-12)
    inverted = vol.skew_30(_smile_chain(slope=0.4), 0.25)
    assert inverted["skew25_30"] < 0
