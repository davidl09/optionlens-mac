from datetime import date, timedelta

import numpy as np
import pytest

from optionlens.analytics import calendar as cal
from optionlens.analytics import stats
from optionlens.models import Bar


def mk(dates, closes, opens=None, highs=None, lows=None, vols=None, sym="T"):
    n = len(closes)
    opens = opens or closes
    highs = highs or [max(o, c) for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    vols = vols or [1000] * n
    return [Bar(sym, d, o, h, l, c, v) for d, o, h, l, c, v in zip(dates, opens, highs, lows, closes, vols)]


def tdays(start, n):
    out, d = [], start
    while len(out) < n:
        if cal.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# ---- returns ---------------------------------------------------------------------------------
def test_returns_row_windows_and_ytd():
    ds = tdays(date(2023, 1, 3), 800)
    c = np.array([100.0 * 1.001 ** i for i in range(800)])
    r = stats.returns_row(ds, c)
    assert r["d1"] == pytest.approx(0.001)
    assert r["d5"] == pytest.approx(1.001 ** 5 - 1)
    assert r["m1"] == pytest.approx(1.001 ** 21 - 1)
    assert r["y1"] == pytest.approx(1.001 ** 252 - 1)
    assert r["y3"] == pytest.approx(1.001 ** 756 - 1)
    last_year_idx = max(i for i, d in enumerate(ds) if d.year < ds[-1].year)
    assert r["ytd"] == pytest.approx(c[-1] / c[last_year_idx] - 1)
    assert list(r) == ["d1", "d5", "m1", "m3", "m6", "ytd", "y1", "y3"]


def test_returns_row_short_history_is_none():
    ds = tdays(date(2024, 1, 2), 10)
    r = stats.returns_row(ds, np.linspace(10, 11, 10))
    assert r["d5"] is not None and r["m1"] is None and r["y3"] is None and r["ytd"] is None


# ---- beta / corr -------------------------------------------------------------------------------
def test_beta_corr_exact_linear_relation():
    rng = np.random.default_rng(1)
    rb = rng.normal(0, 0.01, 300)
    ra = 1.5 * rb
    ds = tdays(date(2023, 1, 3), 301)
    bc = 100 * np.exp(np.concatenate([[0], np.cumsum(rb)]))
    ac = 50 * np.exp(np.concatenate([[0], np.cumsum(ra)]))
    beta, corr = stats.beta_corr(ds, ac, ds, bc)
    assert beta == pytest.approx(1.5)
    assert corr == pytest.approx(1.0)


def test_beta_corr_aligns_dates_and_requires_obs():
    ds = tdays(date(2023, 1, 3), 30)
    c = np.linspace(100, 110, 30)
    beta, corr = stats.beta_corr(ds[:10], c[:10], ds, c)
    assert beta is None and corr is None


# ---- RSI / ATR / SMA -------------------------------------------------------------------------
WILDER = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
          46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]


def _rsi_reference(c, n=14):
    d = np.diff(c)
    g = np.clip(d, 0, None)
    l = np.clip(-d, 0, None)
    ag, al = g[:n].mean(), l[:n].mean()
    out = [100 - 100 / (1 + ag / al)]
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out.append(100 - 100 / (1 + ag / al))
    return out


def test_rsi_wilder_textbook():
    s = stats.rsi_series(np.array(WILDER))
    assert np.all(np.isnan(s[:14]))
    # StockCharts' Wilder example (unrounded averages, as TA-Lib): 70.46, 66.25, 66.48, 69.35, 66.29, 57.92
    np.testing.assert_allclose(s[14:], [70.46, 66.25, 66.48, 69.35, 66.29, 57.92], atol=0.006)
    np.testing.assert_allclose(s[14:], _rsi_reference(np.array(WILDER)), rtol=1e-12)
    assert stats.rsi(np.array(WILDER)) == pytest.approx(s[-1])


def test_rsi_edge_cases():
    assert stats.rsi(np.arange(10.0)) is None
    assert stats.rsi(np.arange(1.0, 30.0)) == 100.0
    assert stats.rsi(np.full(30, 5.0)) == 50.0


def test_atr_pct_constant_range():
    c = np.full(30, 100.0)
    h, l = c + 1, c - 1
    assert stats.atr_pct(h, l, c) == pytest.approx(0.02)
    # gaps dominate true range: close alternates 100/104, H=L=C -> TR=4
    c2 = np.array([100.0, 104.0] * 15)
    assert stats.atr_pct(c2, c2, c2) == pytest.approx(4 / c2[-1])
    assert stats.atr_pct(c[:10], c[:10], c[:10]) is None


def test_sma():
    c = np.arange(1.0, 201.0)
    assert stats.sma(c, 20) == pytest.approx(np.mean(c[-20:]))
    assert stats.sma(c, 201) is None
    s = stats.sma_series(c, 3)
    assert np.isnan(s[1]) and s[2] == pytest.approx(2.0) and s[-1] == pytest.approx(199.0)


def test_high_low_and_fallback():
    c = np.array([10.0, 12, 11, 9, 10])
    h = np.array([10.5, np.nan, 11.5, 9.5, 10.5])
    lo = np.array([9.5, 11.5, 10.5, 8.5, np.nan])
    assert stats.high_low(h, lo, c, 252) == (12.0, 8.5)
    assert stats.high_low(h, lo, c, 2) == (10.5, 8.5)


def test_relative_and_avg_volume():
    v = np.array([100.0] * 30 + [300.0])
    assert stats.relative_volume(v) == pytest.approx(3.0)
    assert stats.avg_volume(v, 30) == pytest.approx((100 * 29 + 300) / 30)
    assert stats.avg_volume(v, 30, exclude_last=True) == pytest.approx(100.0)


def test_support_resistance_fractals_and_clustering():
    # zig-zag with pivot highs at 110, 110.5 (cluster) and 120; pivot lows at 90, 80
    h = np.array([100, 101, 110, 101, 100, 102, 110.5, 102, 100, 105, 120, 105, 100, 99, 98, 99, 100, 100, 100], float)
    lo = np.array([99, 99, 99, 95, 90, 95, 99, 99, 99, 99, 99, 99, 99, 95, 80, 95, 99, 99, 99], float)
    c = np.full(len(h), 100.0)
    sup, res = stats.support_resistance(h, lo, c)
    assert res == [pytest.approx(110.25), pytest.approx(120.0)]
    assert sup == [pytest.approx(90.0), pytest.approx(80.0)]


# ---- gaps & price action ------------------------------------------------------------------------
def test_gap_signal():
    ds = tdays(date(2024, 3, 1), 2)
    b = mk(ds, [100.0, 106.0], opens=[100.0, 105.0])
    g = stats.gap_signal(b)
    assert g["gap_pct"] == pytest.approx(0.05) and g["continued"] is True
    b = mk(ds, [100.0, 104.0], opens=[100.0, 105.0])
    assert stats.gap_signal(b)["continued"] is False


def test_price_action_rows_stats_and_overnight():
    ds = tdays(date(2024, 1, 2), 4)
    closes = [100.0, 102.0, 99.0, 99.0]
    opens = [100.0, 101.0, 103.0, 98.0]
    highs = [101.0, 103.0, 104.0, 100.0]
    lows = [99.0, 100.0, 98.0, 97.0]
    pa = stats.price_action(mk(ds, closes, opens, highs, lows, [10, 20, 30, 40]))
    rows = pa["rows"]
    assert [r["date"] for r in rows] == ds[::-1]
    newest = rows[0]
    assert newest["change_pct"] == pytest.approx(0.0)
    assert newest["gap_pct"] == pytest.approx(98 / 99 - 1)
    assert newest["range_pct"] == pytest.approx(3 / 99)
    assert rows[-1]["change_pct"] is None and rows[-1]["range_pct"] == pytest.approx(0.02)
    st = pa["stats"]
    chg = [0.02, 99 / 102 - 1, 0.0]
    assert st["days"] == 4
    assert st["avg_abs_move"] == pytest.approx(np.mean(np.abs(chg)))
    assert st["up_day_pct"] == pytest.approx(1 / 3)
    assert st["largest_up"]["date"] == ds[1] and st["largest_down"]["date"] == ds[2]
    assert st["stdev_daily"] == pytest.approx(np.std(chg, ddof=1))
    io = pa["intraday_overnight"]
    on = [101 / 100 - 1, 103 / 102 - 1, 98 / 99 - 1]
    intra = [102 / 101 - 1, 99 / 103 - 1, 99 / 98 - 1]
    assert io["avg_overnight"] == pytest.approx(np.mean(on))
    assert io["cum_intraday"] == pytest.approx(np.prod([1 + x for x in intra]) - 1)
    assert io["overnight_up_pct"] == pytest.approx(2 / 3)
    assert len(io["series"]) == 3
    # overnight × intraday compounding reproduces close-to-close
    assert (1 + io["cum_overnight"]) * (1 + io["cum_intraday"]) == pytest.approx(99 / 100)


def test_price_action_start_filter_uses_full_history_for_rel_volume():
    ds = tdays(date(2024, 1, 2), 40)
    b = mk(ds, [100.0] * 40, vols=[100] * 39 + [250])
    pa = stats.price_action(b, start=ds[35])
    assert pa["stats"]["days"] == 5
    assert pa["rows"][0]["rel_volume"] == pytest.approx(2.5)


# ---- seasonality ----------------------------------------------------------------------------------
def test_seasonality_constructed():
    # month-end closes grow 1% every month except March which falls 2%
    ds = cal.trading_days(date(2019, 12, 1), date(2024, 6, 14))
    closes, level, cur = [], 100.0, None
    for d in ds:
        if cur != (d.year, d.month):
            cur = (d.year, d.month)
            level *= 0.98 if d.month == 3 else 1.01
        closes.append(level)
    s = stats.seasonality(mk(ds, closes), today=date(2024, 6, 14))
    assert s["current_month"] == 6
    assert s["years"] == [2020, 2021, 2022, 2023, 2024]
    by = {m["month"]: m for m in s["by_month"]}
    # month m return = last close of m / last close of m-1 - 1 = level change applied at start of m
    assert by[3]["avg"] == pytest.approx(-0.02) and by[3]["pos_pct"] == 0.0 and by[3]["n"] == 5
    assert by[1]["avg"] == pytest.approx(0.01) and by[1]["median"] == pytest.approx(0.01)
    assert by[6]["n"] == 4  # June 2024 is incomplete and excluded
    assert by[12]["n"] == 4  # 2020..2023
    tbl = {(r["year"], r["month"]): r["ret"] for r in s["monthly"]}
    assert tbl[(2024, 6)] is None and tbl[(2022, 3)] == pytest.approx(-0.02)
    assert len(s["by_weekday"]) == 5 and sum(w["n"] for w in s["by_weekday"]) > 0


def test_seasonality_weekday():
    ds = tdays(date(2024, 1, 1), 50)
    closes, c = [], 100.0
    for d in ds:
        c *= 1.01 if d.weekday() == 0 else 1.0
        closes.append(c)
    s = stats.seasonality(mk(ds, closes), today=date(2024, 3, 20))
    mon = s["by_weekday"][0]
    assert mon["avg"] == pytest.approx(0.01) and mon["pos_pct"] == 1.0
    assert s["by_weekday"][2]["avg"] == pytest.approx(0.0)


def test_vol_seasonality():
    ds = cal.trading_days(date(2022, 1, 3), date(2023, 12, 29))
    rng = np.random.default_rng(3)
    closes = list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(ds)))))
    iv = {date(2023, 1, 5): 0.30, date(2023, 1, 6): 0.20, date(2023, 2, 1): 0.25}
    rows = stats.vol_seasonality(iv, mk(ds, closes), today=date(2023, 12, 31))
    assert len(rows) == 12
    jan = rows[0]
    assert jan["avg_iv30"] == pytest.approx(0.25) and jan["iv_samples"] == 2
    assert rows[2]["avg_iv30"] is None and rows[2]["iv_samples"] == 0
    # HV20 needs 20 returns: January 2022 has none, so only 2023 contributes to January
    assert jan["hv_years"] == 1 and rows[11]["hv_years"] == 2
    assert 0.1 < jan["avg_hv20"] < 0.25
