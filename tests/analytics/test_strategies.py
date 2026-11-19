import math
from datetime import date, datetime

import numpy as np
import pytest

from optionlens.analytics import bs, probability as prob, strategies as st
from optionlens.analytics.calendar import NY, year_fraction_to_expiry
from optionlens.analytics.chain import ChainArrays

from .helpers import contract

NOW = datetime(2026, 1, 5, 16, 0, tzinfo=NY)
E1 = date(2026, 2, 4)    # 30 DTE
E2 = date(2026, 3, 6)    # 60 DTE
S0, R, Q, HV = 100.0, 0.04, 0.01, 0.22


def _book():
    """Hand-made chain: spot 100, strikes 90..110 step 5, IVs and feed deltas from BSM."""
    rows = []
    for e in (E1, E2):
        T = year_fraction_to_expiry(e, NOW)
        for k in (90, 95, 100, 105, 110):
            iv = 0.30 - 0.5 * (k / S0 - 1)
            for right in "CP":
                p = bs.price(S0, k, T, R, Q, iv, right)
                g = bs.greeks(S0, k, T, R, Q, iv, right)
                bid, ask = round(p - 0.05, 2), round(p + 0.05, 2)
                rows.append(contract(e, k, right, bid, ask, iv=iv, delta=g["delta"], gamma=g["gamma"],
                                     theta=g["theta"], vega=g["vega"], volume=100, oi=1000))
    return ChainArrays.from_contracts(rows, S0, NOW, symbol="TEST", r=R, q=Q, hv30=HV, iv_rank=0.5,
                                      next_earnings=date(2026, 2, 20))


@pytest.fixture(scope="module")
def ca():
    return _book()


def _q(ca, e, k, right):
    i = next(i for i, c in enumerate(ca.src) if c.expiry == e and c.strike == k and c.right == right)
    return ca.src[i]


P = st.ScreenParams(dte_min=20, dte_max=40)
T1 = year_fraction_to_expiry(E1, NOW)
ATM1 = 0.30   # strikes bracket spot exactly at 100: ATM IV = IV(100)
MU = R - Q


def _pa(k, sigma=ATM1, mu=MU):
    return prob.prob_above(S0, k, T1, sigma, mu)


# ----------------------------------------------------------------------------- kernel
def test_linear_profile_bull_call_spread():
    out = st.linear_profile(np.array([[100.0, 110.0]]), [True, True], [1, -1], 0.0, np.array([4.0]),
                            S0, np.array([T1]), np.array([0.3]), MU, 0.2)
    assert out["max_profit"][0] == pytest.approx(6.0) and out["max_loss"][0] == pytest.approx(4.0)
    assert out["breakevens"][0][0] == pytest.approx(104.0) and np.isnan(out["breakevens"][0][1:]).all()
    assert out["pop"][0] == pytest.approx(_pa(104.0), abs=1e-12)
    assert out["prob_max_profit"][0] == pytest.approx(_pa(110.0), abs=1e-12)
    ev = prob.expected_call(S0, 100, T1, 0.3, MU) - prob.expected_call(S0, 110, T1, 0.3, MU) - 4.0
    assert out["ev_iv"][0] == pytest.approx(ev, abs=1e-12)
    ev_hv = prob.expected_call(S0, 100, T1, 0.2, 0) - prob.expected_call(S0, 110, T1, 0.2, 0) - 4.0
    assert out["ev_hv"][0] == pytest.approx(ev_hv, abs=1e-12)


def test_linear_profile_matches_grid_engine():
    """Exact kink-based PoP/EV equal the numerical grid integration (2001 points, ±6σ)."""
    cases = [  # (K, is_call, qty, stock, debit)
        ([90, 95, 105, 110], [False, False, True, True], [1, -1, -1, 1], 0.0, -2.0),   # short condor
        ([95, 100, 105], [True, True, True], [1, -2, 1], 0.0, 1.0),                    # long fly
        ([100, 100], [True, False], [1, 1], 0.0, 8.0),                                 # straddle
        ([105], [True], [-1], 1.0, 98.0),                                              # covered call
    ]
    for K, c, qty, stock, debit in cases:
        out = st.linear_profile(np.array([K], float), c, qty, stock, np.array([debit]), S0,
                                np.array([T1]), np.array([0.3]), MU)
        z, Pg = prob.grid(S0, T1, 0.3, MU)
        Kc = np.array(K, float)
        intr = np.where(np.array(c), np.maximum(Pg[:, None] - Kc, 0), np.maximum(Kc - Pg[:, None], 0))
        pay = (intr * np.array(qty)).sum(axis=1) + stock * Pg - debit
        pop, ev = prob.pop_ev_on_grid(z, pay)
        assert out["pop"][0] == pytest.approx(pop, abs=2e-5)
        assert out["ev_iv"][0] == pytest.approx(ev, abs=2e-3)


def test_linear_profile_families():
    k = lambda *a: np.array([a], float)  # noqa: E731
    one = np.array([T1]), np.array([0.3])
    ic = st.linear_profile(k(90, 95, 105, 110), [False, False, True, True], [1, -1, -1, 1], 0.0, np.array([-2.0]), S0, *one, MU)
    assert ic["max_profit"][0] == pytest.approx(2) and ic["max_loss"][0] == pytest.approx(3)
    assert list(ic["breakevens"][0][:2]) == pytest.approx([93, 107])
    assert ic["pop"][0] == pytest.approx(_pa(93) - _pa(107), abs=1e-12)
    assert ic["prob_max_profit"][0] == pytest.approx(_pa(95) - _pa(105), abs=1e-12)
    fly = st.linear_profile(k(95, 100, 105), [True] * 3, [1, -2, 1], 0.0, np.array([1.0]), S0, *one, MU)
    assert fly["max_profit"][0] == pytest.approx(4) and fly["max_loss"][0] == pytest.approx(1)
    assert list(fly["breakevens"][0][:2]) == pytest.approx([96, 104])
    assert fly["prob_max_profit"][0] == pytest.approx(0.0, abs=1e-12)
    strad = st.linear_profile(k(100, 100), [True, False], [1, 1], 0.0, np.array([8.0]), S0, *one, MU)
    assert np.isinf(strad["max_profit"][0]) and strad["max_loss"][0] == pytest.approx(8)
    assert strad["pop"][0] == pytest.approx(1 - _pa(92) + _pa(108), abs=1e-12)
    assert np.isnan(strad["prob_max_profit"][0])
    naked = st.linear_profile(k(105), [True], [-1], 0.0, np.array([-2.0]), S0, *one, MU)
    assert np.isinf(naked["max_loss"][0]) and naked["max_profit"][0] == pytest.approx(2)
    assert naked["breakevens"][0][0] == pytest.approx(107)
    cc = st.linear_profile(k(105), [True], [-1], 1.0, np.array([98.0]), S0, *one, MU)
    assert cc["max_profit"][0] == pytest.approx(7) and cc["max_loss"][0] == pytest.approx(98)
    assert cc["breakevens"][0][0] == pytest.approx(98)
    syn = st.linear_profile(k(100, 100), [True, False], [1, -1], 0.0, np.array([1.0]), S0, *one, MU)
    assert np.isinf(syn["max_profit"][0]) and syn["max_loss"][0] == pytest.approx(101)
    assert syn["breakevens"][0][0] == pytest.approx(101)
    short_stock = st.linear_profile(np.zeros((1, 0)), np.zeros(0, bool), np.zeros(0), -1.0, np.array([-100.0]), S0, *one, MU)
    assert np.isinf(short_stock["max_loss"][0]) and short_stock["max_profit"][0] == pytest.approx(100)


# ----------------------------------------------------------------------------- screeners
def test_vertical_credit_natural_vs_mid(ca):
    nat = st.screen("bull-put-spreads", ca, P)
    mid = st.screen("bull-put-spreads", ca, st.ScreenParams(dte_min=20, dte_max=40, pricing="mid"))
    rows = {tuple(r["strikes"]): r for r in nat.rows()}
    rm = {tuple(r["strikes"]): r for r in mid.rows()}
    r = rows[(100.0, 95.0)]
    sp, lp = _q(ca, E1, 100, "P"), _q(ca, E1, 95, "P")
    credit_nat = sp.bid - lp.ask
    credit_mid = sp.mid - lp.mid
    assert r["price"] == pytest.approx(-credit_nat) and r["price_natural"] == pytest.approx(-credit_nat)
    assert r["price_mid"] == pytest.approx(-credit_mid) and rm[(100.0, 95.0)]["price"] == pytest.approx(-credit_mid)
    assert r["is_credit"] and r["cost"] == pytest.approx(-credit_nat * 100)
    assert r["max_profit"] == pytest.approx(credit_nat * 100)
    assert r["max_loss"] == pytest.approx((5 - credit_nat) * 100)
    assert r["breakevens"] == pytest.approx([100 - credit_nat])
    assert r["return_on_risk"] == pytest.approx(credit_nat / (5 - credit_nat))
    assert r["annualized_return"] == pytest.approx(r["return_on_risk"] * 365 / 30)
    assert r["pop"] == pytest.approx(_pa(100 - credit_nat), abs=1e-12)
    assert r["prob_max_profit"] == pytest.approx(_pa(100), abs=1e-12)
    assert r["legs"] == "-1 P 100 / +1 P 95 2026-02-04"
    assert r["dte"] == 30 and r["expiry"] == E1 and r["atm_iv"] == pytest.approx(0.30)
    assert r["delta"] == pytest.approx(100 * (lp.delta - sp.delta))
    assert r["earnings_before_expiry"] is False and r["pricing"] == "natural"
    # widths 5..20 (up to 4 listed strikes): 10 pairs in one expiry
    assert len(nat) == 10


def test_vertical_debit_families(ca):
    bc = {tuple(r["strikes"]): r for r in st.screen("bull-call-spreads", ca, P).rows()}
    r = bc[(100.0, 105.0)]
    lc, sc = _q(ca, E1, 100, "C"), _q(ca, E1, 105, "C")
    debit = lc.ask - sc.bid
    assert r["price"] == pytest.approx(debit) and not r["is_credit"]
    assert r["max_profit"] == pytest.approx((5 - debit) * 100) and r["max_loss"] == pytest.approx(debit * 100)
    assert r["breakevens"] == pytest.approx([100 + debit])
    bp = {tuple(r["strikes"]): r for r in st.screen("bear-put-spreads", ca, P).rows()}
    r = bp[(105.0, 100.0)]
    debit = _q(ca, E1, 105, "P").ask - _q(ca, E1, 100, "P").bid
    assert r["max_loss"] == pytest.approx(debit * 100) and r["breakevens"] == pytest.approx([105 - debit])
    assert r["pop"] == pytest.approx(1 - _pa(105 - debit), abs=1e-12)
    bcs = {tuple(r["strikes"]): r for r in st.screen("bear-call-spreads", ca, P).rows()}
    r = bcs[(100.0, 110.0)]
    credit = _q(ca, E1, 100, "C").bid - _q(ca, E1, 110, "C").ask
    assert r["max_profit"] == pytest.approx(credit * 100) and r["max_loss"] == pytest.approx((10 - credit) * 100)


def test_covered_call_formulas_exact(ca):
    rows = {r["strikes"][0]: r for r in st.screen("covered-calls", ca, P).rows()}
    r = rows[105.0]
    bid = _q(ca, E1, 105, "C").bid
    assert r["static_return"] == pytest.approx(bid / (S0 - bid))
    assert r["if_called_return"] == pytest.approx((105 - S0 + bid) / (S0 - bid))
    assert r["static_annualized"] == pytest.approx(bid / (S0 - bid) * 365 / 30)
    assert r["if_called_annualized"] == pytest.approx((105 - S0 + bid) / (S0 - bid) * 365 / 30)
    assert r["downside_protection"] == pytest.approx(bid / S0)
    assert r["prob_called"] == pytest.approx(_pa(105), abs=1e-12)
    assert r["max_profit"] == pytest.approx((105 - S0 + bid) * 100)
    assert r["max_loss"] == pytest.approx((S0 - bid) * 100)
    assert r["legs"] == "+100 sh / -1 C 105 2026-02-04"
    assert r["delta"] == pytest.approx(100 - 100 * _q(ca, E1, 105, "C").delta)


def test_naked_put_formulas_exact(ca):
    rows = {r["strikes"][0]: r for r in st.screen("naked-puts", ca, P).rows()}
    r = rows[95.0]
    bid = _q(ca, E1, 95, "P").bid
    assert r["return_on_cash"] == pytest.approx(bid / (95 - bid))
    assert r["return_on_cash_annualized"] == pytest.approx(bid / (95 - bid) * 365 / 30)
    assert r["breakevens"] == pytest.approx([95 - bid])
    margin = max(0.20 * S0 - max(0, S0 - 95) + bid, 0.10 * 95 + bid) * 100
    assert r["margin_req"] == pytest.approx(margin)
    assert r["return_on_margin"] == pytest.approx(bid * 100 / margin)
    assert r["prob_otm"] == pytest.approx(_pa(95), abs=1e-12)
    assert r["max_loss"] == pytest.approx((95 - bid) * 100)


def test_long_call_pop_is_n_d2_at_breakeven(ca):
    rows = {r["strikes"][0]: r for r in st.screen("long-calls", ca, P).rows()}
    r = rows[105.0]
    be = 105 + _q(ca, E1, 105, "C").ask
    d2 = (math.log(S0 / be) + (MU - 0.5 * ATM1**2) * T1) / (ATM1 * math.sqrt(T1))
    from scipy.stats import norm
    assert r["pop"] == pytest.approx(norm.cdf(d2), abs=1e-12)
    assert r["max_profit"] is None and r["return_on_risk"] is None
    lp = {r["strikes"][0]: r for r in st.screen("long-puts", ca, P).rows()}[95.0]
    assert lp["max_profit"] == pytest.approx((95 - _q(ca, E1, 95, "P").ask) * 100)


def test_condors_butterflies_straddles(ca):
    ic = st.screen("iron-condors-short", ca, st.ScreenParams(dte_min=20, dte_max=40, max_width_steps=1)).rows()
    sym = {tuple(r["strikes"]) for r in ic}
    assert (90.0, 95.0, 105.0, 110.0) in sym            # put 95 (|Δ|) pairs with the nearest-|Δ| OTM call
    for r in ic:
        k = r["strikes"]
        assert k[1] - k[0] == pytest.approx(k[3] - k[2]) and k[1] < S0 < k[2]
        assert r["max_loss"] == pytest.approx((k[1] - k[0]) * 100 - r["max_profit"])
        assert len(r["breakevens"]) == 2
    ib = st.screen("iron-butterflies-short", ca, P).rows()
    r = next(r for r in ib if r["strikes"] == [95.0, 100.0, 100.0, 105.0])
    credit = (_q(ca, E1, 100, "P").bid + _q(ca, E1, 100, "C").bid - _q(ca, E1, 95, "P").ask - _q(ca, E1, 105, "C").ask)
    assert r["max_profit"] == pytest.approx(credit * 100) and r["max_loss"] == pytest.approx((5 - credit) * 100)
    assert r["breakevens"] == pytest.approx([100 - credit, 100 + credit])
    fly = st.screen("call-butterflies-long", ca, P).rows()
    assert all(r["strikes"][1] - r["strikes"][0] == pytest.approx(r["strikes"][2] - r["strikes"][1]) for r in fly)
    r = next(r for r in fly if r["strikes"] == [95.0, 100.0, 105.0])
    debit = _q(ca, E1, 95, "C").ask - 2 * _q(ca, E1, 100, "C").bid + _q(ca, E1, 105, "C").ask
    assert r["price"] == pytest.approx(debit) and r["max_profit"] == pytest.approx((5 - debit) * 100)
    assert r["legs"] == "+1 C 95 / -2 C 100 / +1 C 105 2026-02-04"
    strad = st.screen("atm-straddles", ca, st.ScreenParams(dte_min=0, dte_max=90)).rows()
    assert [r["strikes"] for r in strad] == [[100.0, 100.0], [100.0, 100.0]]
    debit = _q(ca, E1, 100, "C").ask + _q(ca, E1, 100, "P").ask
    assert strad[0]["breakevens"] == pytest.approx([100 - debit, 100 + debit]) and strad[0]["max_profit"] is None
    short = st.screen("atm-straddles", ca, st.ScreenParams(dte_min=0, dte_max=40, side="short")).rows()[0]
    assert short["max_loss"] is None and short["max_profit"] == pytest.approx(
        (_q(ca, E1, 100, "C").bid + _q(ca, E1, 100, "P").bid) * 100)
    sg = st.screen("strangles", ca, P).rows()
    assert all(r["strikes"][0] > S0 > r["strikes"][1] for r in sg)


def test_calendar_metrics(ca):
    rows = st.screen("calendar-calls", ca, P).rows()
    r = next(r for r in rows if r["strikes"] == [100.0, 100.0])
    f, b = _q(ca, E1, 100, "C"), _q(ca, E2, 100, "C")
    assert r["back_expiry"] == E2 and r["legs"] == "-1 C 100 2026-02-04 / +1 C 100 2026-03-06"
    debit = b.ask - f.bid
    assert r["price"] == pytest.approx(debit) and r["max_loss"] == pytest.approx(debit * 100)
    tf, tb = year_fraction_to_expiry(E1, NOW), year_fraction_to_expiry(E2, NOW)
    assert r["front_iv"] == pytest.approx(0.30) and r["iv_ratio"] == pytest.approx(1.0)
    assert r["forward_vol"] == pytest.approx(0.30)
    assert r["theta_ratio"] == pytest.approx(f.theta / b.theta)
    est = bs.price(100, 100, tb - tf, R, Q, 0.30, "C") - debit
    assert r["est_max_profit"] == pytest.approx(est * 100)
    assert r["max_profit"] == pytest.approx(est * 100, rel=1e-9)
    assert len(r["breakevens"]) == 2 and r["breakevens"][0] < 100 < r["breakevens"][1]
    assert 0 < r["pop"] < 1 and r["earnings_before_expiry"] is True   # earnings 2026-02-20 before back expiry
    assert r["theta"] > 0


def test_synthetic_long(ca):
    rows = {r["strikes"][0]: r for r in st.screen("synthetic-long", ca, P).rows()}
    r = rows[100.0]
    c, p = _q(ca, E1, 100, "C"), _q(ca, E1, 100, "P")
    assert r["synthetic_price_natural"] == pytest.approx(100 + c.ask - p.bid)
    assert r["synthetic_price_mid"] == pytest.approx(100 + c.mid - p.mid)
    carry = S0 * math.exp(-Q * T1) - 100 * math.exp(-R * T1)
    assert r["parity_gap_mid"] == pytest.approx(carry - (c.mid - p.mid))
    assert r["parity_gap_natural"] == pytest.approx(carry - (c.ask - p.bid))
    assert r["max_profit"] is None and r["max_loss"] == pytest.approx((100 + c.ask - p.bid) * 100)


def test_liquidity_and_earnings_filters():
    ca = _book()
    rows = list(ca.src)
    # make the 95 put illiquid in three different ways
    base = [c for c in rows if not (c.expiry == E1 and c.strike == 95 and c.right == "P")]
    p95 = next(c for c in rows if c.expiry == E1 and c.strike == 95 and c.right == "P")
    from dataclasses import replace
    for bad, params in ((replace(p95, oi=5), st.ScreenParams(dte_min=20, dte_max=40, min_oi=10)),
                        (replace(p95, bid=0.0), P),
                        (replace(p95, bid=0.5, ask=2.0), st.ScreenParams(dte_min=20, dte_max=40, max_spread_pct=0.5)),
                        (replace(p95, volume=1), st.ScreenParams(dte_min=20, dte_max=40, min_volume=2))):
        c2 = ChainArrays.from_contracts(base + [bad], S0, NOW, r=R, q=Q, hv30=HV)
        res = st.screen("bull-put-spreads", c2, params)
        assert all(95.0 not in r["strikes"] for r in res.rows())
    with_earn = ChainArrays.from_contracts(rows, S0, NOW, r=R, q=Q, next_earnings=date(2026, 1, 20))
    inc = st.screen("long-calls", with_earn, st.ScreenParams(dte_min=0, dte_max=90, earnings_filter="include"))
    exc = st.screen("long-calls", with_earn, st.ScreenParams(dte_min=0, dte_max=90, earnings_filter="exclude"))
    assert len(inc) == 10 and exc is None
    d = st.screen("long-calls", ca, st.ScreenParams(dte_min=20, dte_max=40, delta_min=0.3, delta_max=0.6)).rows()
    assert all(0.3 <= _q(ca, E1, r["strikes"][0], "C").delta <= 0.6 for r in d) and len(d) >= 1
    assert all(r["ev_hv"] is not None for r in d)
    no_hv = ChainArrays.from_contracts(rows, S0, NOW, r=R, q=Q)
    assert all(r["ev_hv"] is None for r in st.screen("long-calls", no_hv, P).rows())


def test_unprofitable_candidates_dropped(ca):
    res = st.screen("call-butterflies-short", ca, P)
    assert res is None or all(r["max_profit"] > 0 for r in res.rows())


def test_screen_universe_sort_and_truncate(ca):
    other = ChainArrays.from_contracts(list(ca.src), S0, NOW, symbol="OTHER", r=R, q=Q, hv30=0.5)
    rows, tr = st.screen_universe("bull-put-spreads", [ca, other], P, sort_key="ev_hv", limit=5)
    assert tr and len(rows) == 5
    evs = [r["ev_hv"] for r in rows]
    assert evs == sorted(evs, reverse=True)
    rows, tr = st.screen_universe("bull-put-spreads", [ca, other], P, limit=None)
    assert not tr and len(rows) == 20 and {r["symbol"] for r in rows} == {"TEST", "OTHER"}


def test_every_screener_runs_and_keys_consistent(ca):
    common = {"symbol", "strategy", "legs", "expiry", "back_expiry", "dte", "spot", "strikes", "price",
              "price_natural", "price_mid", "pricing", "cost", "is_credit", "max_profit", "max_loss",
              "breakevens", "breakeven_low", "breakeven_high", "return_on_risk", "annualized_return", "pop",
              "prob_max_profit", "ev_iv", "ev_hv", "delta", "gamma", "theta", "vega", "iv", "atm_iv",
              "iv_rank", "hv30", "earnings_before_expiry", "next_earnings", "min_leg_oi", "min_leg_volume",
              "max_leg_spread_pct"}
    params = st.ScreenParams(dte_min=20, dte_max=40, pricing="mid")
    for sid in st.SCREENER_IDS:
        res = st.screen(sid, ca, params)
        assert res is not None and len(res) > 0, sid
        for r in res.rows():
            assert common <= set(r), sid
            assert r["strategy"] == sid and 0 <= r["pop"] <= 1


# ----------------------------------------------------------------------------- templates & legs
def test_templates_cover_spec():
    ids = {t["id"] for t in st.strategy_templates()}
    assert set(st.SCREENERS.values()) <= ids
    for extra in ("long-stock", "short-stock", "protective-put", "short-straddle", "short-strangle",
                  "short-call", "calendar-call-short", "calendar-put-short"):
        assert extra in ids
    ic = next(t for t in st.strategy_templates() if t["id"] == "iron-condor-short")
    assert [l["strike_offset_pct"] for l in ic["legs"]] == pytest.approx([-0.10, -0.05, 0.05, 0.10])
    assert [l["qty"] for l in ic["legs"]] == [1, -1, -1, 1]
    cal = next(t for t in st.strategy_templates() if t["id"] == "calendar-put")
    assert [l["expiry_index"] for l in cal["legs"]] == [0, 1]
    cc = next(t for t in st.strategy_templates() if t["id"] == "covered-call")
    assert cc["legs"][0] == {"kind": "stock", "qty": 100}


def test_resolve_strikes_rules():
    grid = np.arange(80, 121, 2.5)
    sf = lambda ei, right: grid  # noqa: E731
    t = st.TEMPLATES
    assert st.resolve_strikes(t["bull-call-spread"], 101.0, sf) == [100.0, 105.0]
    assert st.resolve_strikes(t["iron-condor-short"], 100.0, sf, "pct_otm", 0.05, 0.05) == [90.0, 95.0, 105.0, 110.0]
    assert st.resolve_strikes(t["long-strangle"], 100.0, sf, "pct_otm", 0.10) == [110.0, 90.0]
    assert st.resolve_strikes(t["covered-call"], 100.0, sf) == [None, 100.0]
    # width collapsing onto the anchor is pushed one listed strike outward
    assert st.resolve_strikes(t["bull-put-spread"], 100.0, sf, width_pct=0.001) == [100.0, 97.5]
    dfn = lambda ei, right, ks: bs.delta(100.0, ks, 0.1, 0.0, 0.0, 0.3, right)  # noqa: E731
    k = st.resolve_strikes(t["long-call"], 100.0, sf, "delta", 0.25, delta_for=dfn)[0]
    ds = np.abs(dfn(0, "C", grid))
    assert k == grid[np.argmin(np.abs(ds - 0.25))]


def test_describe_and_value_legs():
    legs = [st.Leg("option", -1, "C", 200, date(2026, 12, 18), 3.0, 0.3),
            st.Leg("option", 1, "C", 210, date(2026, 12, 18), 1.0, 0.28)]
    assert st.describe_legs(legs) == "-1 C 200 / +1 C 210 2026-12-18"
    assert st.net_cost(legs) == pytest.approx(-200.0)
    v = st.value_at(legs, [190, 205, 220], st.close_dt(date(2026, 12, 18)), 0.04, 0.0)
    assert list(v) == pytest.approx([0, -500, -1000])
    mixed = [st.Leg("stock", 100, price=50.0), st.Leg("option", 1, "P", 47.5, date(2027, 1, 15), 1.2, 0.3)]
    assert st.describe_legs(mixed) == "+100 sh / +1 P 47.5 2027-01-15"
    g = st.position_greeks(mixed, 50.0, NOW, 0.04, 0.0)
    T = year_fraction_to_expiry(date(2027, 1, 15), NOW)
    assert g["delta"] == pytest.approx(100 + 100 * bs.delta(50, 47.5, T, 0.04, 0.0, 0.3, "P"))
    with pytest.raises(ValueError):
        st.value_at([st.Leg("option", 1, "C", 100, date(2027, 1, 15), 1.0, None)], [100], NOW, 0, 0)


def test_rank_trade_ideas(ca):
    ideas = st.rank_trade_ideas(ca, P, n=5, seasonality={"month": 1, "avg": 0.02, "median": 0.01, "pos_pct": 0.7, "n": 10})
    assert set(ideas) == {"symbol", "by_edge", "by_win_rate"}
    edges = [i["edge"] for i in ideas["by_edge"] if i["family"] != "seasonality"]
    assert edges == sorted(edges, reverse=True) and len(edges) == 5
    for i in ideas["by_edge"][:-1]:
        assert i["edge"] == pytest.approx(i["ev_hv"] / i["max_loss"])
        assert set(i) == {"strategy", "label", "family", "legs", "expiry", "cost", "max_profit", "max_loss",
                          "pop", "ev_hv", "edge", "note"}
    pops = [i["pop"] for i in ideas["by_win_rate"][:-1]]
    assert pops == sorted(pops, reverse=True)
    assert ideas["by_edge"][-1]["family"] == "seasonality" and ideas["by_edge"][-1]["pop"] == 0.7
    only = st.rank_trade_ideas(ca, P, n=3, families=["vertical"])
    assert {i["family"] for i in only["by_edge"]} == {"vertical"}
    risks = [i["max_loss"] for i in ideas["by_edge"] + ideas["by_win_rate"] if i["family"] != "seasonality"]
    assert all(r >= st.MIN_IDEA_RISK for r in risks)            # no near-zero-risk (locked quote) ideas
