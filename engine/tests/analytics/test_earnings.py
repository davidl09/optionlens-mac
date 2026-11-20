import math
from datetime import date

import numpy as np
import pytest

from optionlens.analytics import calendar as cal
from optionlens.analytics import earnings as er
from optionlens.models import Bar, EarningsEvent

DAYS = cal.trading_days(date(2025, 6, 2), date(2025, 12, 31))
# special opens (gaps) keyed by date
GAPS = {
    date(2025, 10, 15): 1.10,   # big gap the day after an unknown-timing event on 10-14
    date(2025, 10, 14): 1.01,
    date(2025, 9, 9): 0.92,     # big gap on the event day of an unknown-timing event
    date(2025, 9, 10): 1.005,
}


def series():
    closes = {d: 100.0 + 0.5 * i for i, d in enumerate(DAYS)}
    bars = []
    for i, d in enumerate(DAYS):
        prev = closes[DAYS[i - 1]] if i else closes[d]
        o = prev * GAPS.get(d, 1.0)
        c = closes[d]
        bars.append(Bar("T", d, o, max(o, c) + 1.0, min(o, c) - 1.0, c, 1000))
    return bars, closes


def ev(d, timing, **kw):
    return EarningsEvent("T", d, timing, kw.get("fq"), kw.get("act"), kw.get("est"), kw.get("sur"), None)


def test_reaction_date_rules_with_holidays():
    bars, _ = series()
    # amc Wednesday before Thanksgiving -> Friday (Thanksgiving closed)
    assert er.reaction_date(date(2025, 11, 26), "amc", bars) == (date(2025, 11, 28), False)
    # bmo on the July 4 holiday (Friday) -> rolled forward to Monday
    assert er.reaction_date(date(2025, 7, 4), "bmo", bars) == (date(2025, 7, 7), False)
    # amc on Friday before Labor Day -> Tuesday
    assert er.reaction_date(date(2025, 8, 29), "amc", bars) == (date(2025, 9, 2), False)
    assert er.reaction_date(date(2025, 11, 26), "bmo", bars) == (date(2025, 11, 26), False)
    # unknown: next-day gap bigger -> amc; event-day gap bigger -> bmo
    assert er.reaction_date(date(2025, 10, 14), "unknown", bars) == (date(2025, 10, 15), True)
    assert er.reaction_date(date(2025, 9, 9), "unknown", bars) == (date(2025, 9, 9), True)
    # future event: no bars -> None
    assert er.reaction_date(date(2026, 1, 28), "amc", bars)[0] is None
    assert er.reaction_date(date(2025, 12, 31), "amc", bars)[0] is None


def test_event_rows_moves_and_drifts():
    bars, closes = series()
    events = [ev(date(2025, 11, 26), "amc", fq="Q3", act=1.2, est=1.0, sur=0.2),
              ev(date(2025, 10, 14), "unknown"),
              ev(date(2025, 9, 9), "unknown"),
              ev(date(2026, 1, 28), "amc")]
    react = date(2025, 11, 28)
    pre_day = date(2025, 11, 26)
    iv = {pre_day: 0.40, react: 0.28}
    rows = er.event_rows(events, bars, implied_moves={date(2025, 11, 26): 0.05}, iv30_by_date=iv)
    assert [r["date"] for r in rows] == [date(2025, 11, 26), date(2025, 10, 14), date(2025, 9, 9)]
    r = rows[0]
    pc = closes[pre_day]
    assert r["reaction_date"] == react and r["timing"] == "amc" and not r["timing_inferred"]
    assert r["pre_close"] == pc
    assert r["move_pct"] == pytest.approx(closes[react] / pc - 1)
    assert r["gap_pct"] == pytest.approx(0.0)
    hi, lo = closes[react] + 1.0, pc - 1.0  # open == prev close here
    assert r["max_move_pct"] == pytest.approx(max(abs(hi / pc - 1), abs(lo / pc - 1)))
    for n in (5, 10):
        assert r[f"pre_drift_{n}"] == pytest.approx(pc / closes[cal.add_trading_days(pre_day, -n)] - 1)
    for n in (1, 5, 10, 20):
        exp = closes[cal.add_trading_days(react, n)] / closes[react] - 1 if cal.add_trading_days(react, n) in closes else None
        assert r[f"post_drift_{n}"] == (pytest.approx(exp) if exp is not None else None)
    assert r["post_drift_20"] is not None  # 20 trading days after 11-28 is still in December
    assert r["implied_move_pct"] == 0.05 and r["iv30_before"] == 0.40 and r["iv30_after"] == 0.28
    assert r["eps_actual"] == 1.2 and r["surprise_pct"] == 0.2 and r["fiscal_quarter"] == "Q3"
    u = rows[1]
    assert u["timing_inferred"] and u["reaction_date"] == date(2025, 10, 15)
    assert u["gap_pct"] == pytest.approx(0.10)
    assert u["implied_move_pct"] is None and u["iv30_before"] is None
    assert er.event_rows(events, bars, limit=1)[0]["date"] == date(2025, 11, 26)


def test_earnings_stats():
    rows = [{"move_pct": 0.05, "gap_pct": 0.04, "pre_drift_5": 0.01, "post_drift_5": 0.02, "post_drift_20": None},
            {"move_pct": -0.10, "gap_pct": -0.08, "pre_drift_5": -0.01, "post_drift_5": None, "post_drift_20": 0.03},
            {"move_pct": 0.02, "gap_pct": None, "pre_drift_5": None, "post_drift_5": 0.04, "post_drift_20": 0.01},
            {"move_pct": None, "gap_pct": None}]
    s = er.earnings_stats(rows)
    mv = [0.05, -0.10, 0.02]
    assert s["count"] == 3
    assert s["avg_move"] == pytest.approx(np.mean(mv))
    assert s["avg_abs_move"] == pytest.approx(np.mean(np.abs(mv)))
    assert s["median_abs_move"] == pytest.approx(0.05)
    assert s["stdev_move"] == pytest.approx(np.std(mv, ddof=1))
    assert s["up_pct"] == pytest.approx(2 / 3)
    assert s["max_up"] == 0.05 and s["max_down"] == -0.10
    assert s["avg_abs_gap"] == pytest.approx(0.06)
    assert s["avg_pre_drift_5"] == pytest.approx(0.0)
    assert s["avg_post_drift_5"] == pytest.approx(0.03)
    assert s["avg_post_drift_20"] == pytest.approx(0.02)
    empty = er.earnings_stats([])
    assert empty["count"] == 0 and all(v is None for k, v in empty.items() if k != "count")


def test_implied_earnings_move_and_excess():
    exps = [{"expiry": date(2025, 11, 28), "straddle": 8.0, "atm_iv": 0.60, "t": 0.03},
            {"expiry": date(2025, 11, 21), "straddle": 5.0, "atm_iv": 0.50, "t": 0.01},
            {"expiry": date(2025, 12, 5), "straddle": 9.0, "atm_iv": 0.35, "t": 0.05}]
    out = er.implied_earnings_move(exps, date(2025, 11, 28), 100.0)
    assert out["expiry_used"] == date(2025, 11, 28)
    assert out["implied_move_pct"] == pytest.approx(0.08)
    assert out["excess_implied_move_pct"] == pytest.approx(math.sqrt(0.36 * 0.03 - 0.35 ** 2 * (0.03 - 1 / 252)))
    # no later expiry -> no excess; nothing after reaction -> all None
    last = er.implied_earnings_move(exps, date(2025, 12, 1), 100.0)
    assert last["expiry_used"] == date(2025, 12, 5) and last["excess_implied_move_pct"] is None
    none = er.implied_earnings_move(exps, date(2026, 1, 1), 100.0)
    assert none == {"implied_move_pct": None, "excess_implied_move_pct": None, "expiry_used": None}
    # back vol above front: event variance clipped at 0
    flat = [{"expiry": date(2025, 11, 28), "straddle": None, "atm_iv": 0.2, "t": 0.03},
            {"expiry": date(2025, 12, 5), "straddle": 1.0, "atm_iv": 0.5, "t": 0.05}]
    f = er.implied_earnings_move(flat, date(2025, 11, 28), 100.0)
    assert f["implied_move_pct"] is None and f["excess_implied_move_pct"] == 0.0


def test_implied_vs_hist_ratio():
    assert er.implied_vs_hist_ratio(0.06, 0.04) == pytest.approx(1.5)
    assert er.implied_vs_hist_ratio(None, 0.04) is None
    assert er.implied_vs_hist_ratio(0.06, 0.0) is None
