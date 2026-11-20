import math
from datetime import date

import numpy as np
import pytest

from optionlens.analytics import calendar as cal
from optionlens.analytics import backtest as bt
from optionlens.analytics.stats import bar_arrays
from optionlens.models import Bar, EarningsEvent


def bars_from(closes, start=date(2024, 1, 2), opens=None, sym="T"):
    ds = cal.trading_days(start, date(2035, 1, 1))[: len(closes)]
    opens = opens or closes
    return [Bar(sym, d, o, max(o, c), min(o, c), c, 1000) for d, o, c in zip(ds, opens, closes)]


def growth_bars(n=300, g=0.01, sym="T"):
    return bars_from([100.0 * (1 + g) ** i for i in range(n)], sym=sym)


# ---- build_result ----------------------------------------------------------------------------
def test_build_result_drawdown_cagr_sharpe():
    eq = [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 120.0), (date(2024, 1, 3), 90.0), (date(2025, 1, 1), 130.0)]
    trades = [dict(symbol="T", entry_date=date(2024, 1, 2), exit_date=date(2024, 1, 3), legs="long T",
                   entry_value=1, exit_value=2, pnl=1.0, ret=0.10, pricing="real", note=None),
              dict(symbol="T", entry_date=date(2024, 1, 3), exit_date=date(2024, 1, 10), legs="long T",
                   entry_value=1, exit_value=2, pnl=-1.0, ret=-0.05, pricing="model", note=None)]
    r = bt.build_result(trades, eq, None, ["n"])
    s = r["summary"]
    assert s["max_drawdown"] == pytest.approx(-0.25)
    assert s["cagr"] == pytest.approx(1.3 ** (365.25 / 366) - 1)
    ch = np.array([0.2, -0.25, 130 / 90 - 1])
    assert s["sharpe"] == pytest.approx(ch.mean() / ch.std(ddof=1) * math.sqrt(252))
    assert s["total_return"] == pytest.approx(0.30)                 # equity end / start − 1
    assert s["win_rate"] == 0.5 and s["total_pnl"] == 0.0
    assert s["real_count"] == 1 and s["model_count"] == 1
    assert s["avg_hold_days"] == pytest.approx((1 + 5) / 2)
    assert r["benchmark"] is None and r["notes"] == ["n"]
    assert r["equity"][1] == {"date": date(2024, 1, 2), "value": 120.0}
    assert sum(h["count"] for h in r["histogram"]) == 2 and len(r["histogram"]) == 5


def test_build_result_flat_equity_and_empty():
    eq = [(cal.trading_days(date(2024, 1, 2), date(2024, 3, 1))[i], 100 * 1.01 ** i) for i in range(30)]
    s = bt.build_result([], eq, None, [])["summary"]
    assert s["sharpe"] is None  # constant daily return: zero dispersion
    assert s["max_drawdown"] == 0.0 and s["count"] == 0 and s["win_rate"] is None
    assert s["avg_return"] is None and s["total_return"] is None and s["total_pnl"] is None
    empty = bt.build_result([], [], None, [])
    assert empty["histogram"] == [] and empty["summary"]["cagr"] is None


# ---- stock backtester ----------------------------------------------------------------------------
def test_constant_growth_every_n_days_hold():
    bars = growth_bars(300)
    r = bt.backtest_stock({"T": bars}, {"type": "every_n_days", "n": 10}, {"hold_days": 5})
    t = r["trades"]
    assert len(t) == 30
    assert all(x["ret"] == pytest.approx(1.01 ** 5 - 1) for x in t)
    assert all(x["note"] == "hold 5d" and x["pricing"] == "real" and x["legs"] == "long T" for x in t)
    assert t[0]["entry_date"] == bars[0].date and t[0]["exit_date"] == bars[5].date
    assert t[0]["pnl"] == pytest.approx(bars[5].close - bars[0].close)
    s = r["summary"]
    assert s["total_return"] == pytest.approx(1.01 ** 150 - 1)
    assert s["win_rate"] == 1.0 and s["max_drawdown"] == 0.0 and s["avg_hold_days"] == 5.0
    eq = r["equity"]
    assert len(eq) == 300 and eq[0]["value"] == 10000.0
    assert eq[-1]["value"] == pytest.approx(10000 * 1.01 ** 150)
    span = (bars[-1].date - bars[0].date).days
    assert s["cagr"] == pytest.approx(1.01 ** (150 * 365.25 / span) - 1)
    held = np.zeros(299)
    for i in range(0, 300, 10):
        held[i:i + 5] = 0.01
    assert s["sharpe"] == pytest.approx(held.mean() / held.std(ddof=1) * math.sqrt(252))
    assert r["benchmark"][-1]["value"] == pytest.approx(10000 * 1.01 ** 299)
    assert r["benchmark"][0]["value"] == pytest.approx(10000)


def test_default_hold_and_open_at_end():
    bars = growth_bars(8)
    r = bt.backtest_stock({"T": bars}, {"type": "every_n_days", "n": 6}, {})
    t = r["trades"]
    assert [x["note"] for x in t] == ["hold 5d", "open at end"]
    assert t[1]["entry_date"] == bars[6].date and t[1]["exit_date"] == bars[7].date


def test_stop_loss_and_take_profit_first_hit():
    bars = bars_from([100.0, 103.0, 97.0, 106.0, 100.0])
    r = bt.backtest_stock({"T": bars}, {"type": "every_n_days", "n": 100},
                          {"stop_loss": 0.02, "take_profit": 0.05, "hold_days": 4})
    assert r["trades"][0]["note"] == "stop loss" and r["trades"][0]["exit_value"] == 97.0
    r = bt.backtest_stock({"T": bars}, {"type": "every_n_days", "n": 100},
                          {"stop_loss": 0.05, "take_profit": 0.025})
    assert r["trades"][0]["note"] == "take profit" and r["trades"][0]["exit_date"] == bars[1].date
    r = bt.backtest_stock({"T": bars}, {"type": "every_n_days", "n": 100},
                          {"stop_loss": 0.05, "take_profit": 0.055})
    assert r["trades"][0]["note"] == "take profit" and r["trades"][0]["exit_value"] == 106.0


def test_short_side():
    bars = bars_from([100.0, 90.0, 80.0])
    r = bt.backtest_stock({"T": bars}, {"type": "every_n_days", "n": 100}, {"hold_days": 2}, side="short")
    t = r["trades"][0]
    assert t["ret"] == pytest.approx(0.2) and t["pnl"] == pytest.approx(20.0) and t["legs"] == "short T"
    assert r["equity"][-1]["value"] == pytest.approx(12000.0)
    assert r["summary"]["total_return"] == pytest.approx(0.2)
    # short stop loss triggers on a rise
    up = bars_from([100.0, 104.0, 110.0])
    r = bt.backtest_stock({"T": up}, {"type": "every_n_days", "n": 100}, {"stop_loss": 0.05}, side="short")
    assert r["trades"][0]["note"] == "stop loss" and r["trades"][0]["exit_value"] == 110.0


def test_start_end_window_and_indicator_warmup():
    bars = growth_bars(50)
    r = bt.backtest_stock({"T": bars}, {"type": "every_n_days", "n": 5}, {"hold_days": 2},
                          start=bars[10].date, end=bars[20].date)
    assert r["trades"][0]["entry_date"] == bars[10].date
    assert r["trades"][-1]["exit_date"] <= bars[20].date
    assert r["equity"][0]["date"] == bars[10].date and r["equity"][-1]["date"] == bars[20].date


def test_earnings_relative_entries_around_thanksgiving():
    ds = cal.trading_days(date(2025, 11, 3), date(2025, 12, 31))
    closes = [100.0 + i for i in range(len(ds))]
    bars = [Bar("T", d, c, c, c, c, 1) for d, c in zip(ds, closes)]
    evs = {"T": [EarningsEvent("T", date(2025, 11, 26), "amc", None, None, None, None, None)]}
    r = bt.backtest_stock({"T": bars}, {"type": "days_before_earnings", "n": 1},
                          {"at_earnings_reaction": True}, earnings_by_symbol=evs)
    t = r["trades"]
    assert len(t) == 1
    assert t[0]["entry_date"] == date(2025, 11, 26) and t[0]["exit_date"] == date(2025, 11, 28)
    assert t[0]["note"] == "earnings reaction"
    r = bt.backtest_stock({"T": bars}, {"type": "days_before_earnings", "n": 2}, {"hold_days": 1},
                          earnings_by_symbol=evs)
    assert r["trades"][0]["entry_date"] == date(2025, 11, 25) and r["trades"][0]["exit_date"] == date(2025, 11, 26)
    r = bt.backtest_stock({"T": bars}, {"type": "days_after_earnings", "n": 1}, {"hold_days": 1},
                          earnings_by_symbol=evs)
    assert r["trades"][0]["entry_date"] == date(2025, 12, 1)
    r = bt.backtest_stock({"T": bars}, {"type": "days_after_earnings", "n": 0}, {"hold_days": 1},
                          earnings_by_symbol=evs)
    assert r["trades"][0]["entry_date"] == date(2025, 11, 28)


def test_unknown_timing_earnings_entries_do_not_peek_at_the_gap():
    ds = cal.trading_days(date(2025, 11, 3), date(2025, 12, 31))
    # realized: the big gap is on the event day (looks bmo), but timing is unknown before the event
    bars = [Bar("T", d, c, c, c, c, 1) for d, c in ((d, 120.0 if d >= date(2025, 11, 12) else 100.0) for d in ds)]
    evs = {"T": [EarningsEvent("T", date(2025, 11, 12), "unknown", None, None, None, None, None)]}
    r = bt.backtest_stock({"T": bars}, {"type": "days_before_earnings", "n": 1},
                          {"at_earnings_reaction": True}, earnings_by_symbol=evs)
    (t,) = r["trades"]
    assert t["entry_date"] == date(2025, 11, 11)          # before the earlier candidate (event day)
    assert t["exit_date"] == date(2025, 11, 13)           # after the later candidate (next session)
    assert any("unknown timing" in n for n in r["notes"])
    r = bt.backtest_stock({"T": bars}, {"type": "days_after_earnings", "n": 0}, {"hold_days": 1},
                          earnings_by_symbol=evs)
    assert r["trades"][0]["entry_date"] == date(2025, 11, 13)


def test_multi_symbol_equal_weight_equity_and_benchmark():
    a = growth_bars(10, 0.01, "A")
    b = growth_bars(10, 0.0, "B")
    r = bt.backtest_stock({"A": a, "B": b}, {"type": "every_n_days", "n": 100}, {"hold_days": 9})
    assert len(r["trades"]) == 2
    assert r["equity"][-1]["value"] == pytest.approx(10000 * 1.005 ** 9)
    assert r["benchmark"][-1]["value"] == pytest.approx(10000 * (1.01 ** 9 + 1) / 2)
    # overlapping trades of two symbols: total return follows the equal-weight equity, not Π(1+ret)
    assert r["summary"]["total_return"] == pytest.approx(1.005 ** 9 - 1)


# ---- entry rules ----------------------------------------------------------------------------------
def _sig(bars, entry, lo=0):
    return np.nonzero(bt.entry_signals(bar_arrays(bars), entry, [], lo))[0].tolist()


def test_entry_rule_gap():
    bars = bars_from([100.0, 100.0, 100.0, 100.0], opens=[100.0, 103.0, 100.0, 96.0])
    assert _sig(bars, {"type": "gap", "direction": "up", "value": 0.02}) == [1]
    assert _sig(bars, {"type": "gap", "direction": "down", "value": 0.03}) == [3]


def test_entry_rule_sma_cross_and_rsi_cross():
    closes = [10.0] * 5 + [9.0, 9.0, 12.0, 13.0, 8.0]
    bars = bars_from(closes)
    # SMA(3) at i=7 = (9+9+12)/3=10 < 12 and at i=6 close 9 <= sma 9.33 -> cross above at 7
    assert _sig(bars, {"type": "sma_cross", "n": 3, "direction": "above"}) == [7]
    assert _sig(bars, {"type": "sma_cross", "n": 3, "direction": "below"}) == [5, 9]
    rng = np.random.default_rng(5)
    c = list(100 + np.cumsum(rng.normal(0, 1, 200)))
    from optionlens.analytics.stats import rsi_series
    r = rsi_series(np.array(c))
    got = _sig(bars_from(c), {"type": "rsi", "direction": "below", "value": 40})
    exp = [i for i in range(15, 200) if r[i] < 40 <= r[i - 1]]
    assert got == exp and len(exp) > 0


def test_entry_rule_calendar_rules_and_new_high():
    bars = growth_bars(300)
    dom = _sig(bars, {"type": "day_of_month", "day": 15})
    assert all(bars[i].date.day >= 15 for i in dom)
    assert len({(bars[i].date.year, bars[i].date.month) for i in dom}) == len(dom)
    moy = _sig(bars, {"type": "month_of_year", "month": 3})
    assert [bars[i].date for i in moy] == [date(2024, 3, 1), date(2025, 3, 3)]
    nh = _sig(bars, {"type": "new_52w", "direction": "high"})
    assert nh[0] == 252 and nh == list(range(252, 300))
    assert _sig(bars, {"type": "new_52w", "direction": "low"}) == []
    with pytest.raises(ValueError):
        _sig(bars, {"type": "nope"})
