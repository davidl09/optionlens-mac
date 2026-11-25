"""IV surface (analytics.surface) on a synthetic chain with a known smile: bucket interpolation in delta and
log-moneyness, strike rows, modes, reliability masking (unreliable contracts never feed a reliable cell), IV layers
and the earnings expiry."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime

import pytest

from optionlens.analytics import surface
from optionlens.analytics.calendar import NY
from optionlens.analytics.chain import ChainArrays

from .helpers import contract

ASOF = datetime(2026, 11, 19, 16, 0, tzinfo=NY)
E0, E1, E2, FAR = date(2026, 11, 19), date(2026, 12, 18), date(2027, 1, 15), date(2028, 1, 21)
# strike: (IV, put delta, call delta) — puts below spot and calls above are the OTM sides
SMILE = {80: (0.40, -0.10, 0.90), 85: (0.35, -0.20, 0.80), 90: (0.30, -0.30, 0.70), 95: (0.27, -0.40, 0.60),
         100: (0.25, -0.48, 0.52), 105: (0.24, -0.60, 0.40), 110: (0.23, -0.70, 0.30), 115: (0.22, -0.80, 0.20),
         120: (0.215, -0.90, 0.10)}


def _chain(overrides: dict | None = None, *, iv_shift: float = 0.0, drop: tuple = (), **kw) -> ChainArrays:
    """Four expiries (0 DTE, 29, 57 and > 366 DTE) of the SMILE at spot 100; ``overrides`` {(expiry, strike, right):
    fields} change single contracts, ``drop`` lists (expiry, strike) not listed."""
    rows = []
    for e in (E0, E1, E2, FAR):
        for k, (iv, pd, cd) in SMILE.items():
            if (e, k) in drop:
                continue
            for right, d in (("C", cd), ("P", pd)):
                c = contract(e, k, right, 1.0, 1.1, iv=iv + iv_shift, delta=d, oi=100)
                rows.append(replace(c, **(overrides or {}).get((e, k, right), {})))
    return ChainArrays.from_contracts(rows, 100.0, ASOF, **kw)


def _col(s: dict, e: date) -> int:
    return [x["expiry"] for x in s["expiries"]].index(e)


def _cell(s: dict, row, e: date, cells=None) -> dict:
    return (cells or s["cells"])[s["rows"].index(row)][_col(s, e)]


def test_columns_rows_and_delta_interpolation():
    s = surface.build(_chain(), "delta")
    assert [x["expiry"] for x in s["expiries"]] == [E1, E2]          # 0 DTE and > 1 year left out
    assert s["rows"] == list(surface.DELTA_ROWS) and s["rows"][8] == surface.ATM_DELTA
    assert _cell(s, -0.15, E1)["iv"] == pytest.approx(0.375)           # between the 10Δ (0.40) and 20Δ (0.35) puts
    assert _cell(s, -0.25, E1)["iv"] == pytest.approx(0.325)
    assert _cell(s, 0.35, E1)["iv"] == pytest.approx(0.235)            # between the 40Δ (0.24) and 30Δ (0.23) calls
    atm = _cell(s, 0.5, E1)
    assert atm["iv"] == pytest.approx(0.25) == s["expiries"][0]["atm_iv"] and atm["strike"] == 100 and atm["right"] == "C"
    ten = _cell(s, -0.10, E1)
    assert ten["reliable"] and ten["reason"] is None and (ten["strike"], ten["right"]) == (80, "P")
    assert ten["osi"] == f"TEST{E1:%y%m%d}P00080000" and (ten["bid"], ten["ask"], ten["oi"]) == (1.0, 1.1, 100)
    assert all(c["reliable"] for r in s["cells"] for c in r)


def test_moneyness_rows_interpolate_in_log_moneyness_over_otm_contracts():
    s = surface.build(_chain(), "moneyness")
    assert s["rows"][0] == -0.20 and s["rows"][-1] == 0.20 and len(s["rows"]) == 17
    assert _cell(s, -0.10, E1)["iv"] == pytest.approx(0.30)            # exactly the 90 put
    w = (math.log(0.875) - math.log(0.85)) / (math.log(0.90) - math.log(0.85))
    assert _cell(s, -0.125, E1)["iv"] == pytest.approx(0.35 + w * (0.30 - 0.35))
    assert _cell(s, -0.125, E1)["right"] == "P" and _cell(s, 0.125, E1)["right"] == "C"
    out = _cell(s, -0.20, E1)
    assert out["iv"] == pytest.approx(0.40) and out["reliable"]        # the 80 put is the last quoted strike
    s = surface.build(_chain(drop=((E1, 80),)), "moneyness")
    gone = _cell(s, -0.20, E1)
    assert gone["iv"] is None and not gone["reliable"] and gone["reason"] == surface.OUT_OF_RANGE   # never extrapolated


def test_strike_rows_take_the_otm_contract_and_null_where_not_listed():
    s = surface.build(_chain(drop=((E2, 120),)), "strike")
    assert s["rows"] == [80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0]
    assert (_cell(s, 85.0, E1)["right"], _cell(s, 85.0, E1)["iv"]) == ("P", 0.35)
    assert (_cell(s, 110.0, E1)["right"], _cell(s, 110.0, E1)["iv"]) == ("C", 0.23)
    missing = _cell(s, 120.0, E2)
    assert missing["iv"] is None and missing["osi"] is None and missing["reason"] == surface.NOT_LISTED


def test_unreliable_contracts_do_not_feed_interpolation():
    # the 20Δ put has no bid and an outlier IV; the 30Δ call a 67% spread and an outlier IV
    ca = _chain({(E1, 85, "P"): {"bid": 0.0, "iv": 0.80}, (E1, 110, "C"): {"bid": 1.0, "ask": 2.0, "iv": 0.60}})
    s = surface.build(ca, "delta")
    assert _cell(s, -0.15, E1)["iv"] == pytest.approx(0.40 + 0.25 * (0.30 - 0.40))   # 10Δ .. 30Δ, not the outlier
    assert _cell(s, -0.15, E1)["reliable"] and _cell(s, -0.15, E1)["strike"] != 85
    assert _cell(s, 0.35, E1)["iv"] == pytest.approx(0.24 + 0.25 * (0.22 - 0.24))    # 40Δ .. 20Δ calls
    assert _cell(s, -0.15, E2)["iv"] == pytest.approx(0.375)                         # other expiries untouched
    k = surface.build(ca, "strike")
    assert not _cell(k, 85.0, E1)["reliable"]
    assert _cell(k, 85.0, E1)["reason"] == "no bid" and _cell(k, 85.0, E1)["iv"] == pytest.approx(0.80)
    assert _cell(k, 110.0, E1)["reason"].startswith("spread 67% of mid")


def test_a_cell_reliable_contracts_cannot_give_falls_back_greyed_with_the_reason():
    s = surface.build(_chain({(E1, 80, "P"): {"oi": 0}}), "delta")
    ten = _cell(s, -0.10, E1)
    assert ten["iv"] == pytest.approx(0.40) and not ten["reliable"] and ten["reason"] == "no open interest"
    assert ten["strike"] == 80
    reasons = surface.contract_reasons(_chain({(E1, 80, "P"): {"iv": None}, (E1, 85, "P"): {"ask": 0.0}}))
    assert {"no IV", "no ask"} <= set(reasons) and None in set(reasons)


def test_iv_layers_source_first_computed_only_for_an_expiry_without_source_ivs():
    flags = {(E1, 90, "P"): {"iv_source": "computed_mid", "iv": 0.90}}
    flags.update({(E2, k, r): {"iv_source": "computed_mid"} for k in SMILE for r in "CP"})
    s = surface.build(_chain(flags), "delta")
    assert [x["iv_computed"] for x in s["expiries"]] == [False, True]
    assert _cell(s, -0.25, E2)["reliable"] and _cell(s, -0.25, E2)["iv"] == pytest.approx(0.325)
    k = surface.build(_chain(flags), "strike")
    assert not _cell(k, 90.0, E1)["reliable"] and _cell(k, 90.0, E1)["reason"].startswith("engine-computed IV")
    assert _cell(s, -0.30, E1)["iv"] == pytest.approx(0.35 + 0.5 * (0.27 - 0.35))  # 20Δ .. 40Δ, not the computed 30Δ


def test_modes():
    ca = _chain()
    s = surface.build(ca, "delta")
    iv = surface.with_mode(s, "iv")
    assert _cell(s, -0.15, E1, iv)["value"] == _cell(s, -0.15, E1)["iv"]
    vs = surface.with_mode(s, "vs_atm")
    assert all(c["value"] == 0 for c in vs[s["rows"].index(0.5)])                     # the ATM row is zero
    assert _cell(s, -0.25, E1, vs)["value"] == pytest.approx(0.325 - 0.25)
    assert all(c["value"] is None for r in surface.with_mode(s, "chg_1d") for c in r)   # no prior chain
    prior = surface.build(_chain(iv_shift=-0.01), "delta", expiries=[E1, E2])
    chg = surface.with_mode(s, "chg_1d", prior)
    assert all(c["value"] == pytest.approx(0.01) for r in chg for c in r)
    assert s["cells"][0][0]["value"] == s["cells"][0][0]["iv"]                        # the built surface is untouched
    bad_prior = surface.build(_chain({(E1, 80, "P"): {"bid": 0.0}}, iv_shift=-0.01), "delta", expiries=[E2, E1])
    c = _cell(s, -0.10, E1, surface.with_mode(s, "chg_1d", bad_prior))
    assert not c["reliable"] and c["reason"] == "prior day: no bid" and c["value"] == pytest.approx(0.01)
    with pytest.raises(ValueError):
        surface.with_mode(s, "nope")


def test_strike_rows_of_a_prior_chain_follow_todays_strikes_and_columns():
    s = surface.build(_chain(), "strike")
    prior = surface.build(_chain(drop=((E1, 120),), iv_shift=-0.02), "strike", strikes=s["rows"], expiries=[E2])
    assert prior["rows"] == s["rows"] and [x["expiry"] for x in prior["expiries"]] == [E2]
    chg = surface.with_mode(s, "chg_1d", prior)
    assert _cell(s, 120.0, E2, chg)["value"] == pytest.approx(0.02) and _cell(s, 120.0, E1, chg)["value"] is None


def test_the_first_expiry_after_the_earnings_reaction_is_flagged():
    s = surface.build(_chain(next_earnings=date(2026, 12, 18), next_reaction=date(2026, 12, 21)), "delta")
    assert [x["earnings_expiry"] for x in s["expiries"]] == [False, True]   # amc on expiry day reacts after it
    s = surface.build(_chain(next_earnings=date(2026, 12, 1), next_reaction=date(2026, 12, 2)), "delta")
    assert [x["earnings_expiry"] for x in s["expiries"]] == [True, False]
    assert not any(x["earnings_expiry"] for x in surface.build(_chain(), "delta")["expiries"])
    with pytest.raises(ValueError):
        surface.build(_chain(), "nope")
