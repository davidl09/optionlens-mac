from datetime import date

import numpy as np
import pytest

from optionlens.analytics import bs, calendar as cal, optbacktest as ob
from optionlens.analytics.strategies import Leg, close_dt
from optionlens.models import Bar, EarningsEvent

R, Q, IV = 0.03, 0.0, 0.30


class FakeProvider:
    def __init__(self, mids=None, iv=IV, expiries=None, strikes=None, spreads=None):
        self.mids = mids or {}
        self.iv = iv
        self.expiries = expiries
        self.strikes = strikes
        self.spreads = spreads or {}
        self.iv_calls = []

    def real_mid(self, d, symbol, expiry, strike, right):
        return self.mids.get((d, expiry, strike, right))

    def iv_proxy(self, d, symbol, tenor_days):
        self.iv_calls.append(tenor_days)
        return self.iv

    def listed_expiries(self, d, symbol):
        return self.expiries or []

    def listed_strikes(self, d, symbol, expiry, right):
        return self.strikes or []

    def real_spread_pct(self, d, symbol, expiry, strike, right):
        return self.spreads.get((d, expiry, strike, right))


def bars_from(closes_by_day):
    return [Bar("TST", d, c, c, c, c, 1000) for d, c in closes_by_day]


def flat_bars(start=date(2025, 1, 2), end=date(2025, 12, 31), price=100.0):
    return bars_from([(d, price) for d in cal.trading_days(start, end)])


def model(spot, k, d, expiry, right, iv=IV, r=R):
    T = cal.year_fraction_to_expiry(expiry, close_dt(d))
    return bs.price(spot, k, T, r, Q, iv, right)


# ----------------------------------------------------------------------------- listing synthesis
def test_strike_grid_and_synth_expiries():
    assert [ob.strike_step(x) for x in (20, 75, 250, 900)] == [1.0, 2.5, 5.0, 10.0]
    assert ob.strike_step(2000, is_index=True) == 5.0 and ob.strike_step(6000, is_index=True) == 25.0
    g = ob.synth_strikes(101.3)
    assert np.allclose(np.diff(g), 5.0) and 100.0 in g and g[0] <= 50.65 + 5
    ex = ob.synth_expiries(date(2026, 3, 25))
    assert date(2026, 4, 2) in ex and date(2026, 4, 3) not in ex   # Good Friday -> Thursday
    assert date(2026, 3, 27) in ex and all(e > date(2026, 3, 25) for e in ex)
    assert date(2026, 12, 18) in ex and date(2026, 12, 11) not in ex  # beyond 9 weeks only monthlies
    assert all(cal.is_trading_day(e) for e in ex)


def test_risk_basis():
    exp = date(2025, 3, 21)
    assert ob.risk_basis([Leg("option", -1, "P", 95, exp)], [2.0], 100.0) == pytest.approx((95 - 2) * 100)
    assert ob.risk_basis([Leg("option", 1, "C", 100, exp)], [3.0], 100.0) == pytest.approx(300)
    assert ob.risk_basis([Leg("stock", -100)], [100.0], 100.0) == pytest.approx(10000)
    assert ob.risk_basis([Leg("stock", 100), Leg("option", -1, "C", 100, exp)], [100.0, 3.0], 100.0) == pytest.approx(9700)
    # short straddle: unlimited -> naked margin of both short legs
    m = ob.risk_basis([Leg("option", -1, "C", 100, exp), Leg("option", -1, "P", 100, exp)], [3.0, 2.5], 100.0)
    assert m == pytest.approx((0.2 * 100 + 3.0) * 100 + (0.2 * 100 + 2.5) * 100)
    ic = [Leg("option", 1, "P", 90, exp), Leg("option", -1, "P", 95, exp), Leg("option", -1, "C", 105, exp),
          Leg("option", 1, "C", 110, exp)]
    assert ob.risk_basis(ic, [0.5, 1.2, 1.1, 0.4], 100.0) == pytest.approx((5 - 1.4) * 100)


# ----------------------------------------------------------------------------- /backtest/options
def test_long_call_to_expiry_model_pricing_flat_price():
    bars = flat_bars()
    prov = FakeProvider()
    res = ob.backtest_options("TST", bars, "long-call", provider=prov, rate=R, target_dte=30,
                              entry={"type": "monthly"}, exit={"type": "expiry"},
                              start=date(2025, 1, 2), end=date(2025, 6, 30))
    tr = res["trades"]
    assert len(tr) == 6
    t0 = tr[0]
    assert t0["entry_date"] == date(2025, 1, 2)
    # nearest synthesized expiry to 30 DTE from 2025-01-02: Fri 2025-01-31
    assert t0["exit_date"] == date(2025, 1, 31) and t0["note"] == "expiry"
    assert t0["legs"] == "+1 C 100 2025-01-31"
    assert t0["entry_value"] == pytest.approx(100 * model(100, 100, date(2025, 1, 2), date(2025, 1, 31), "C"))
    assert t0["exit_value"] == 0.0 and t0["ret"] == pytest.approx(-1.0)
    assert t0["pricing"] == "model"
    s = res["summary"]
    assert s["count"] == 6 and s["win_rate"] == 0.0 and s["model_count"] == 6 and s["real_count"] == 0
    assert s["total_pnl"] == pytest.approx(sum(t["pnl"] for t in tr))
    eq = res["equity"]
    assert eq[0]["date"] == date(2025, 1, 2)
    # capital = peak basis in use: monthly entries overlap the previous 30-DTE trade -> 2 bases
    capital = eq[0]["value"]
    assert capital == pytest.approx(2 * max(t["entry_value"] for t in tr), rel=0.05)
    assert eq[-1]["value"] == pytest.approx(capital + s["total_pnl"])
    assert s["total_return"] == pytest.approx(s["total_pnl"] / capital)
    assert res["benchmark"][0]["value"] == pytest.approx(capital) and res["benchmark"][-1]["value"] == pytest.approx(capital)
    assert s["max_drawdown"] < 0


def test_real_vs_model_tagging_and_strike_snapping():
    bars = flat_bars(price=101.3)
    d0, exp = date(2025, 1, 2), date(2025, 1, 31)
    # real mids at entry for the snapped ATM strike 100 (grid $5)
    mids = {(d0, exp, 100.0, "C"): 2.5}
    prov = FakeProvider(mids=mids)
    res = ob.backtest_options("TST", bars, "long-call", provider=prov, rate=R, target_dte=30,
                              entry={"type": "monthly"}, exit={"type": "expiry"}, start=d0, end=date(2025, 1, 31))
    t = res["trades"][0]
    assert t["legs"] == "+1 C 100 2025-01-31"
    assert t["entry_value"] == pytest.approx(250.0)
    assert t["exit_value"] == pytest.approx(130.0)                 # intrinsic at expiry close
    assert t["pricing"] == "real"                                  # real entry mid + exact intrinsic
    # dte exit before expiry with no stored mid at exit -> model
    res2 = ob.backtest_options("TST", bars, "long-call", provider=prov, rate=R, target_dte=30,
                               entry={"type": "monthly"}, exit={"type": "dte", "dte": 10}, start=d0, end=date(2025, 1, 31))
    t2 = res2["trades"][0]
    assert t2["note"] == "dte" and t2["exit_date"] == date(2025, 1, 21) and t2["pricing"] == "model"
    assert t2["exit_value"] == pytest.approx(100 * model(101.3, 100, date(2025, 1, 21), exp, "C"))
    # real listing: strikes come from the provider (no snapping to the synthetic grid)
    prov3 = FakeProvider(expiries=[exp, date(2025, 2, 21)], strikes=[99.0, 101.0, 103.0])
    res3 = ob.backtest_options("TST", bars, "long-call", provider=prov3, rate=R, target_dte=30,
                               entry={"type": "monthly"}, start=d0, end=date(2025, 1, 31))
    assert res3["trades"][0]["legs"] == "+1 C 101 2025-01-31"


def test_take_profit_first_hit_on_rising_series():
    days = cal.trading_days(date(2025, 1, 2), date(2025, 3, 31))
    bars = bars_from([(d, 100 * 1.01**i) for i, d in enumerate(days)])
    res = ob.backtest_options("TST", bars, "long-call", provider=FakeProvider(), rate=R, target_dte=30,
                              entry={"type": "every_n_days", "n": 100}, exit={"type": "expiry", "take_profit": 0.5},
                              start=days[0], end=days[-1])
    t = res["trades"][0]
    assert t["note"] == "take profit"
    assert t["pnl"] >= 0.5 * t["entry_value"]
    # the previous close had not reached the target
    prev = days[days.index(t["exit_date"]) - 1]
    exp = date(2025, 1, 31)
    sp = 100 * 1.01 ** days.index(prev)
    assert 100 * model(sp, 100, prev, exp, "C") - t["entry_value"] < 0.5 * t["entry_value"]


def test_stop_loss_on_short_put_falling_series():
    days = cal.trading_days(date(2025, 1, 2), date(2025, 3, 31))
    bars = bars_from([(d, 100 * 0.99**i) for i, d in enumerate(days)])
    res = ob.backtest_options("TST", bars, "short-put", provider=FakeProvider(), rate=R, target_dte=30,
                              entry={"type": "every_n_days", "n": 100}, exit={"type": "expiry", "stop_loss": 0.1},
                              start=days[0], end=days[-1])
    t = res["trades"][0]
    assert t["note"] == "stop loss" and t["ret"] <= -0.1
    basis = t["pnl"] / t["ret"]
    credit = -t["entry_value"]
    assert basis == pytest.approx((100 - credit / 100) * 100)       # max risk of a short put


def test_rolling_straddle_backtest_is_sequential():
    bars = flat_bars(start=date(2023, 1, 3), end=date(2025, 12, 31))
    res = ob.straddle_backtest("TST", bars, provider=FakeProvider(), rate=R, target_dte=30, years=1.0)
    tr = res["trades"]
    assert len(tr) >= 10
    for a, b in zip(tr, tr[1:]):
        assert b["entry_date"] == a["exit_date"]
    assert all(t["note"] == "expiry" for t in tr[:-1]) and tr[-1]["note"] in ("expiry", "open at end")
    assert all(t["legs"].startswith("+1 C 100 / +1 P 100") for t in tr)
    assert all(t["pnl"] < 0 for t in tr)   # flat underlying: straddles expire worthless
    wing = ob.straddle_wing_backtest("TST", bars, provider=FakeProvider(), rate=R, side="short",
                                     wing_pct=0.05, start=date(2025, 1, 2))
    assert wing["trades"] and all(t["pnl"] > 0 for t in wing["trades"])  # short fly keeps the credit
    assert all(t["legs"].startswith("+1 P 95 / -1 P 100 / -1 C 100 / +1 C 105") for t in wing["trades"])


# ----------------------------------------------------------------------------- earnings strategies
def _earn_bars():
    days = cal.trading_days(date(2025, 9, 1), date(2025, 12, 31))
    closes = {d: 100.0 for d in days}
    closes[date(2025, 11, 28)] = 110.0       # reaction day (Fri after Thanksgiving, early close)
    for d in days:
        if d > date(2025, 11, 28):
            closes[d] = 112.0
    return bars_from(sorted(closes.items())), days


def test_earnings_windows_around_thanksgiving():
    bars, _ = _earn_bars()
    ev = EarningsEvent("TST", date(2025, 11, 26), "amc", "Q3", None, None, None, None)
    a = ob.bar_arrays(bars)
    (e, react, d0, d1), = ob.earnings_windows(a, [ev], 12, -1, 1)
    assert react == date(2025, 11, 28) and d0 == date(2025, 11, 26) and d1 == date(2025, 12, 1)
    (_, _, d0, d1), = ob.earnings_windows(a, [ev], 12, -3, 0)
    assert d0 == date(2025, 11, 24) and d1 == date(2025, 11, 28)
    future = EarningsEvent("TST", date(2026, 1, 28), "bmo", None, None, None, None, None)
    assert len(ob.earnings_windows(a, [ev, future], 12, -1, 1)) == 1


def test_earnings_long_stock_and_option_trades():
    bars, _ = _earn_bars()
    ev = EarningsEvent("TST", date(2025, 11, 26), "amc", "Q3", None, None, None, None)
    prov = FakeProvider()
    st = ob.earnings_strategy_trades("TST", bars, [ev], "long-stock", provider=prov, rate=R)
    (t,) = st
    assert t["entry_value"] == pytest.approx(10000) and t["exit_value"] == pytest.approx(11200)
    assert t["ret"] == pytest.approx(0.12) and t["pricing"] == "real" and t["legs"] == "+100 sh"
    short = ob.earnings_strategy_trades("TST", bars, [ev], "short-stock", provider=prov, rate=R)[0]
    assert short["pnl"] == pytest.approx(-1200) and short["ret"] == pytest.approx(-0.12)
    ls = ob.earnings_strategy_trades("TST", bars, [ev], "long-straddle", provider=prov, rate=R)[0]
    # exit 2025-12-01 (Mon): first expiry >= exit is Fri 2025-12-05 (long premium)
    assert ls["legs"] == "+1 C 100 / +1 P 100 2025-12-05" and ls["pricing"] == "model"
    exp = date(2025, 12, 5)
    v0 = 100 * (model(100, 100, date(2025, 11, 26), exp, "C") + model(100, 100, date(2025, 11, 26), exp, "P"))
    v1 = 100 * (model(112, 100, date(2025, 12, 1), exp, "C") + model(112, 100, date(2025, 12, 1), exp, "P"))
    assert ls["entry_value"] == pytest.approx(v0) and ls["exit_value"] == pytest.approx(v1)
    assert ls["ret"] == pytest.approx((v1 - v0) / v0)


def test_short_premium_expiry_rule_and_credit_return_basis():
    days = cal.trading_days(date(2025, 9, 1), date(2025, 12, 31))
    bars = bars_from([(d, 100.0) for d in days])
    # bmo on Thu 2025-12-04 -> reaction Thu; exit +1 = Fri 2025-12-05 (a weekly expiry)
    ev = EarningsEvent("TST", date(2025, 12, 4), "bmo", None, None, None, None, None)
    prov = FakeProvider()
    long_ = ob.earnings_strategy_trades("TST", bars, [ev], "long-straddle", provider=prov, rate=R)[0]
    short = ob.earnings_strategy_trades("TST", bars, [ev], "short-straddle", provider=prov, rate=R)[0]
    assert long_["legs"].endswith("2025-12-05") and long_["exit_value"] == 0.0   # expires at exit: intrinsic
    assert short["legs"].endswith("2025-12-12")                                   # ≥ 2 days after exit
    basis = short["pnl"] / short["ret"]
    credit = -short["entry_value"] / 100
    assert basis == pytest.approx(2 * (0.2 * 100 + credit / 2) * 100, rel=0.05)
    ic = ob.earnings_strategy_trades("TST", bars, [ev], "iron-condor-short", provider=prov, rate=R)[0]
    assert ic["legs"].startswith("+1 P 90 / -1 P 95 / -1 C 105 / +1 C 110")
    cal_ = ob.earnings_strategy_trades("TST", bars, [ev], "calendar-call-long", provider=prov, rate=R)[0]
    assert cal_["legs"] == "-1 C 100 2025-12-12 / +1 C 100 2025-12-19"   # back = next monthly (3rd Fri)


def test_spread_filter_skips_wide_real_quotes():
    bars, _ = _earn_bars()
    ev = EarningsEvent("TST", date(2025, 11, 26), "amc", None, None, None, None, None)
    d0, exp = date(2025, 11, 26), date(2025, 12, 5)
    mids = {(d0, exp, 100.0, "C"): 2.0}
    wide = FakeProvider(mids=mids, spreads={(d0, exp, 100.0, "C"): 0.40})
    ok = FakeProvider(mids=mids, spreads={(d0, exp, 100.0, "C"): 0.10})
    assert ob.earnings_strategy_trades("TST", bars, [ev], "long-call-atm", provider=wide, rate=R) == []
    (t,) = ob.earnings_strategy_trades("TST", bars, [ev], "long-call-atm", provider=ok, rate=R)
    assert t["entry_value"] == pytest.approx(200.0) and t["pricing"] == "model"  # exit leg is model


def test_earnings_summary_rows():
    bars, _ = _earn_bars()
    evs = [EarningsEvent("TST", date(2025, 11, 26), "amc", None, None, None, None, None),
           EarningsEvent("TST", date(2025, 10, 15), "bmo", None, None, None, None, None)]
    res = ob.earnings_strategy_backtest("TST", bars, evs, provider=FakeProvider(), rate=R, lookback=12, entry=-1, exit=1)
    assert res["symbol"] == "TST" and res["entry"] == -1 and res["exit"] == 1
    assert len(res["rows"]) == 30 == len(set(ob.EARNINGS_STRATEGY_IDS))
    keys = {"strategy", "label", "trades", "win_rate", "avg_return", "median_return", "best", "worst", "avg_pnl",
            "real_count", "model_count"}
    for r in res["rows"]:
        assert set(r) == keys and r["trades"] == 2 and r["real_count"] + r["model_count"] == 2
    ls = next(r for r in res["rows"] if r["strategy"] == "long-stock")
    assert ls["win_rate"] == 0.5 and ls["best"] == pytest.approx(0.12) and ls["worst"] == pytest.approx(0.0)
    assert ls["avg_pnl"] == pytest.approx(600.0) and ls["real_count"] == 2
