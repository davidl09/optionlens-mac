"""Engine-computed IV / greeks (analytics.computed_iv) and the one-layer rule of chain IV metrics."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pytest

from optionlens.analytics import bs, chainmetrics as cm, vol
from optionlens.analytics.calendar import NY, year_fraction_to_expiry
from optionlens.analytics.chain import ChainArrays
from optionlens.analytics.computed_iv import fill_iv_greeks
from optionlens.models import OptionContract

SPOT, R, Q = 100.0, 0.04, 0.01
ASOF = datetime(2026, 11, 20, 16, 0, tzinfo=NY)            # Friday close: the prices of a Saturday snapshot
OPEN = datetime(2026, 11, 20, 9, 30, tzinfo=NY)
EXP = date(2026, 12, 18)
T = year_fraction_to_expiry(EXP, ASOF)


def opt(strike=100.0, right="C", expiry=EXP, **kw) -> OptionContract:
    base = dict(osi=f"X{expiry:%y%m%d}{right}{int(strike * 1000):08d}", symbol="X", expiry=expiry, strike=strike,
                right=right, bid=None, ask=None, last=None, change=None, volume=None, oi=None, iv=None, delta=None,
                gamma=None, theta=None, vega=None, rho=None, theo=None)
    return OptionContract(**{**base, **kw})


def fill(*cs):
    return fill_iv_greeks(cs, spot=SPOT, asof=ASOF, r=R, q=Q, session_start=OPEN, source="ibkr")


def model(sigma, strike=100.0, right="C", t=T):
    return bs.price(SPOT, strike, t, R, Q, sigma, right)


def test_iv_from_the_mid_and_greeks_from_it():
    p = model(0.30)
    (c,) = fill(opt(bid=p - 0.05, ask=p + 0.05, last=p - 1, volume=10))
    assert c.iv == pytest.approx(0.30, abs=1e-6) and c.iv_source == "computed_mid"
    g = bs.greeks(SPOT, 100.0, T, R, Q, c.iv, "C")
    assert (c.delta, c.gamma, c.theta, c.vega) == pytest.approx((g["delta"], g["gamma"], g["theta"], g["vega"]))
    assert c.greeks_source == "computed" and c.theo is None          # theo is never invented


def test_iv_from_a_last_of_the_session_only():
    p = model(0.25, 90.0, "P")
    traded = opt(90.0, "P", last=p, last_ts=datetime(2026, 11, 20, 15, 58, tzinfo=NY))
    stale = opt(95.0, "P", last=p, last_ts=datetime(2026, 11, 19, 15, 0, tzinfo=NY), volume=0)
    no_time_volume = opt(90.0, "C", last=model(0.25, 90.0), volume=3)
    no_time_no_volume = opt(110.0, "C", last=1.0, volume=0)
    a, b, c, d = fill(traded, stale, no_time_volume, no_time_no_volume)
    assert a.iv == pytest.approx(0.25, abs=1e-6) and a.iv_source == "computed_last"
    assert (b.iv, b.iv_source, b.delta, b.greeks_source) == (None, None, None, None)   # Thursday's trade: no IV
    assert c.iv == pytest.approx(0.25, abs=1e-6) and c.iv_source == "computed_last"
    assert (d.iv, d.iv_source) == (None, None)


def test_one_sided_quote_falls_back_to_a_current_last_and_unsolvable_stays_null():
    one_side = opt(bid=0.0, ask=None, last=model(0.4), volume=1)
    below_intrinsic = opt(80.0, bid=10.0, ask=10.2)                 # call worth ≥ ~19.9: outside no-arb bounds
    a, b = fill(one_side, below_intrinsic)
    assert a.iv_source == "computed_last" and a.iv == pytest.approx(0.4, abs=1e-6)
    assert (b.iv, b.iv_source, b.delta, b.greeks_source) == (None, None, None, None)


def test_source_values_are_kept_and_labelled():
    full = opt(bid=1.0, ask=1.2, iv=0.33, delta=0.41, gamma=0.02, theta=-0.03, vega=0.1)
    iv_only = opt(105.0, bid=1.0, ask=1.2, iv=0.35)
    a, b = fill(full, iv_only)
    assert (a.iv, a.iv_source, a.delta, a.greeks_source, a.gamma) == (0.33, "ibkr", 0.41, "ibkr", 0.02)
    g = bs.greeks(SPOT, 105.0, T, R, Q, 0.35, "C")
    assert b.iv == 0.35 and b.iv_source == "ibkr" and b.greeks_source == "computed"
    assert b.delta == pytest.approx(g["delta"])


def test_expired_or_without_underlying_nothing_is_computed():
    old = opt(expiry=date(2026, 11, 19), bid=1.0, ask=1.2)
    (a,) = fill(old)
    assert (a.iv, a.iv_source) == (None, None)
    (b,) = fill_iv_greeks([opt(bid=1.0, ask=1.2)], spot=None, asof=ASOF, r=R, q=Q, session_start=OPEN, source="ibkr")
    assert (b.iv, b.delta) == (None, None)


# ----------------------------------------------------------------------------- one IV layer per metric
def _arrays(rows: list[OptionContract]) -> ChainArrays:
    return ChainArrays.from_contracts(rows, SPOT, ASOF, symbol="X")


def _strip(exp, sigma, computed):
    """Calls and puts at 95/100/105 priced at `sigma`; with `computed`, the feed sent no IV (the engine solves it)."""
    t = year_fraction_to_expiry(exp, ASOF)
    out = []
    for k in (95.0, 100.0, 105.0):
        for right in ("C", "P"):
            p = bs.price(SPOT, k, t, R, Q, sigma, right)
            out.append(opt(k, right, exp, bid=p - 0.01, ask=p + 0.01, volume=5, oi=5,
                           iv=None if computed else sigma, delta=None if computed else bs.delta(SPOT, k, t, R, Q, sigma, right)))
    return out


def test_atm_iv_per_expiry_from_one_layer_and_iv30_never_mixes_sources():
    near, far = ASOF.date() + timedelta(days=7), ASOF.date() + timedelta(days=63)
    rows = fill(*_strip(near, 0.60, computed=True), *_strip(far, 0.30, computed=False))
    ca = _arrays(rows)
    term = vol.atm_iv_by_expiry(ca)
    assert [(r["atm_iv_computed"], round(r["atm_iv"], 3)) for r in term] == [(True, 0.6), (False, 0.3)]
    m = cm.metrics_from_arrays(ca)
    # IV30 from the source layer only (flat from the far expiry), not interpolated against the computed 60%
    assert m["iv_computed"] is False and m["iv30"] == pytest.approx(0.30, abs=1e-3)
    assert m["atm_iv_front"] == pytest.approx(0.30, abs=1e-3)
    infos = cm.expiry_infos(ca)
    assert [i["atm_iv_computed"] for i in infos] == [True, False]


def test_a_chain_with_only_computed_ivs_says_so():
    near, far = ASOF.date() + timedelta(days=7), ASOF.date() + timedelta(days=63)
    ca = _arrays(fill(*_strip(near, 0.50, computed=True), *_strip(far, 0.40, computed=True)))
    m = cm.metrics_from_arrays(ca)
    assert m["iv_computed"] is True and 0.40 < m["iv30"] < 0.50
    assert cm.metrics_from_arrays(_arrays([opt()]))["iv_computed"] is None      # no IV at all
    assert np.isnan(ca.iv_layer(False).iv).all() and np.isfinite(ca.iv_layer(True).iv).all()
