"""DbPriceProvider over the seeded option_eod day (2026-11-18) and the §0.3 IV proxy chain."""
from __future__ import annotations

from datetime import date

import pytest

from optionlens.services.context import DbPriceProvider

D, EXP = date(2026, 11, 18), date(2026, 12, 18)


def test_real_quotes_and_listings(client):
    p = DbPriceProvider()
    chain = client.get(f"/api/symbol/AAPL/chain?expiry={EXP}").json()
    row = next(r for r in chain["calls"] if r["strike"] == 350)
    assert p.real_mid(D, "AAPL", EXP, 350.0, "C") == pytest.approx(row["mid"])
    assert p.real_spread_pct(D, "AAPL", EXP, 350.0, "C") == pytest.approx((row["ask"] - row["bid"]) / row["mid"])
    assert EXP in p.listed_expiries(D, "AAPL") and 350.0 in p.listed_strikes(D, "AAPL", EXP, "C")
    assert p.real_mid(date(2026, 11, 17), "AAPL", EXP, 350.0, "C") is None and p.listed_expiries(date(2026, 11, 17), "AAPL") == []


def test_iv_proxy_chain(client):
    p = DbPriceProvider()
    iv30 = client.get("/api/symbol/AAPL/volatility").json()["iv30"]
    assert p.iv_proxy(date(2026, 11, 19), "AAPL", 30) == pytest.approx(iv30)     # stored constant-maturity IV
    hv_day = date(2025, 6, 2)                                                    # no stored IV: HV30 · k
    assert 0.05 < p.iv_proxy(hv_day, "AAPL", 30) < 1.0
    assert p.iv_proxy(date(2001, 1, 2), "AAPL", 30) is None
