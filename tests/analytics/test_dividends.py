from datetime import date

import pytest

from optionlens.analytics import calendar as cal
from optionlens.analytics import dividends as dv
from optionlens.models import Bar, Dividend

ASOF = date(2025, 11, 20)


def div(d, amt, kind="Cash"):
    return Dividend("T", d, amt, None, None, None, kind)


def quarterly():
    out = []
    for y in range(2019, 2026):
        for m in (2, 5, 8, 11):
            out.append(div(cal.add_trading_days(date(y, m, 10), 0), round(0.20 + 0.01 * (y - 2019), 4)))
    out.append(div(date(2024, 12, 16), 1.00, "Special Cash"))
    out.append(div(date(2026, 2, 10), 0.27))  # announced future ex-date
    return out


def test_frequency_and_annual_rate():
    ds = quarterly()
    assert dv.frequency(ds, ASOF) == 4
    assert dv.annual_rate(ds, ASOF) == pytest.approx(0.26 * 4)
    monthly = [div(date(2025, m, 15), 0.1) for m in range(1, 11)]
    assert dv.frequency(monthly) == 12
    semi = [div(date(y, m, 1), 0.5) for y in (2023, 2024) for m in (3, 9)]
    assert dv.frequency(semi) == 2
    annual = [div(date(y, 6, 1), 1.0) for y in (2022, 2023, 2024)]
    assert dv.frequency(annual) == 1
    assert dv.frequency([div(date(2025, 1, 1), 1.0)]) is None
    assert dv.annual_rate([], ASOF) is None


def test_trailing_yield_includes_specials():
    ds = quarterly()
    # (2024-11-20, 2025-11-20]: special 1.00 + four regular 0.26
    assert dv.trailing_yield(ds, 100.0, ASOF) == pytest.approx((1.0 + 4 * 0.26) / 100.0)
    assert dv.trailing_yield(ds, None, ASOF) == 0.0
    assert dv.trailing_yield([], 50.0, ASOF) == 0.0


def test_growth_cagr_of_ttm_regular():
    ds = quarterly()
    assert dv.growth(ds, ASOF, 1) == pytest.approx(1.04 / 1.00 - 1)
    assert dv.growth(ds, ASOF, 3) == pytest.approx((1.04 / 0.92) ** (1 / 3) - 1)
    assert dv.growth(ds, ASOF, 5) == pytest.approx((1.04 / 0.84) ** (1 / 5) - 1)
    assert dv.growth(ds, ASOF, 10) is None  # history does not reach back


def test_is_dividend_increase():
    assert not dv.is_dividend_increase(quarterly(), ASOF)  # Aug 0.26 -> Nov 0.26
    assert dv.is_dividend_increase(quarterly())             # incl. future 0.27
    assert dv.is_dividend_increase([div(date(2025, 1, 1), 0.5), div(date(2025, 4, 1), 0.55),
                                    div(date(2025, 5, 1), 2.0, "special")])
    assert not dv.is_dividend_increase([div(date(2025, 1, 1), 0.5)])


def _bars(pairs):
    return [Bar("T", d, c, c, c, c, 100) for d, c in pairs]


def test_capture_rows_and_recovery():
    d = cal.trading_days(date(2025, 3, 3), date(2025, 3, 31))
    closes = [100.0, 100.0, 99.5, 99.6, 99.9, 100.0, 101.0,      # ex on d[2], recovers on d[5]
              101.0, 100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0]
    bars = _bars(list(zip(d, closes)))
    divs = [div(d[2], 0.26), div(d[8], 0.30), div(date(2025, 6, 2), 0.3)]
    rows = dv.capture_rows(divs, bars)
    assert [r["ex_date"] for r in rows] == [date(2025, 6, 2), d[8], d[2]]
    r1 = rows[2]
    assert r1["pre_close"] == 100.0 and r1["ex_close"] == 99.5
    assert r1["price_return"] == pytest.approx(-0.005)
    assert r1["total_return"] == pytest.approx((99.5 + 0.26) / 100 - 1)
    assert r1["recovery_days"] == 3
    r2 = rows[1]
    assert r2["pre_close"] == 101.0 and r2["recovery_days"] is None
    assert r2["total_return"] == pytest.approx(100.3 / 101 - 1)
    future = rows[0]
    assert future["ex_close"] is None and future["total_return"] is None
    st = dv.capture_stats(rows)
    assert st["count"] == 2
    assert st["avg_total_return"] == pytest.approx(((99.76 / 100 - 1) + (100.3 / 101 - 1)) / 2)
    assert st["win_rate"] == 0.0 and st["avg_recovery_days"] == 3.0


def test_capture_recovered_same_day():
    d = cal.trading_days(date(2025, 3, 3), date(2025, 3, 7))
    rows = dv.capture_rows([div(d[1], 0.5)], _bars(list(zip(d, [50.0, 50.2, 50.0, 50.0, 50.0]))))
    assert rows[0]["recovery_days"] == 0 and rows[0]["total_return"] == pytest.approx(0.7 / 50)


def test_dividend_summary_shape():
    ds = quarterly()
    d = cal.trading_days(date(2025, 10, 1), ASOF)
    bars = _bars([(x, 100.0) for x in d])
    s = dv.dividend_summary("T", ds, bars, 100.0, ASOF)
    assert set(s) == {"symbol", "annual_rate", "yield", "frequency", "growth_1y", "growth_3y", "growth_5y",
                      "next", "history", "capture", "capture_stats"}
    assert s["yield"] == pytest.approx(1.04 / 100)
    assert s["next"] == {"ex_date": date(2026, 2, 10), "amount": 0.27, "days": (date(2026, 2, 10) - ASOF).days,
                         "yield": pytest.approx(0.0104)}
    assert s["history"][0]["ex_date"] == date(2026, 2, 10)
    assert set(s["history"][0]) == {"ex_date", "amount", "pay_date", "record_date", "declared_date", "kind"}
    assert all(r["ex_date"] <= ASOF for r in s["capture"])
    nov = next(r for r in s["capture"] if r["ex_date"] == date(2025, 11, 10))
    assert nov["total_return"] == pytest.approx(0.0026)
    assert dv.dividend_summary("X", [], [], 10.0, ASOF)["next"] is None
