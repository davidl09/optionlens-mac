"""Put-call-parity implied dividend yield and the IV/HV proxy ratio (additive helpers)."""
import math
from datetime import date, datetime

import numpy as np
import pytest

from optionlens.analytics import bs, vol
from optionlens.analytics.calendar import NY, year_fraction_to_expiry
from optionlens.analytics.chain import ChainArrays
from optionlens.analytics.dividends import implied_yield_from_chain

from .helpers import contract

ASOF = datetime(2026, 11, 19, 16, 0, tzinfo=NY)


EXPIRIES = (date(2027, 2, 19), date(2027, 6, 17), date(2027, 12, 17))


def _chain(q: float, r: float = 0.04, spot: float = 100.0, spread: float = 0.02, expiries=EXPIRIES,
           quoted_spot: float | None = None):
    rows = []
    for e in expiries:
        T = year_fraction_to_expiry(e, ASOF)
        for k in np.arange(80, 121, 2.5):
            for right in ("C", "P"):
                px = float(bs.price(spot, k, T, r, q, 0.3, right))
                rows.append(contract(e, float(k), right, px * (1 - spread / 2), px * (1 + spread / 2)))
    return ChainArrays.from_contracts(rows, quoted_spot or spot, ASOF, symbol="TEST")


@pytest.mark.parametrize("q", [0.0, 0.02, 0.05])
def test_recovers_bsm_dividend_yield(q):
    assert implied_yield_from_chain(_chain(q), 0.04) == pytest.approx(q, abs=1e-6)


def test_forward_fit_ignores_stale_spot_quote():
    assert implied_yield_from_chain(_chain(0.03, quoted_spot=100.5), 0.04) == pytest.approx(0.03, abs=1e-6)
    one = _chain(0.03, expiries=(date(2027, 2, 19),))          # single expiry: spot-based estimate
    assert implied_yield_from_chain(one, 0.04) == pytest.approx(0.03, abs=1e-6)


def test_clamped_and_none_without_liquid_pairs():
    assert implied_yield_from_chain(_chain(0.3), 0.04) == pytest.approx(0.15)
    assert implied_yield_from_chain(_chain(0.02, spread=0.6), 0.04) is None        # too wide
    assert implied_yield_from_chain(_chain(0.02, expiries=(date(2026, 12, 4),)), 0.04) is None  # DTE < 60


def test_iv_hv_ratio():
    assert vol.iv_hv_ratio([0.3, 0.2, math.nan, 0.4], [0.2, 0.2, 0.1, 0.0]) == pytest.approx(1.25)
    assert vol.iv_hv_ratio([], []) == 1.15


def test_iv_by_delta_signed_sides():
    e = date(2027, 2, 19)
    rows = [contract(e, 90, "P", 1, 1.1, iv=0.30, delta=-0.20), contract(e, 95, "P", 2, 2.1, iv=0.26, delta=-0.40),
            contract(e, 105, "C", 2, 2.1, iv=0.22, delta=0.40), contract(e, 110, "C", 1, 1.1, iv=0.20, delta=0.20)]
    ca = ChainArrays.from_contracts(rows, 100.0, ASOF, symbol="TEST")
    out = {r["delta"]: r["iv"] for r in vol.iv_by_delta(ca, 0, (-0.3, -0.05, 0.3))}
    assert out[-0.3] == pytest.approx(0.28) and out[0.3] == pytest.approx(0.21) and out[-0.05] is None
