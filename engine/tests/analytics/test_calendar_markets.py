"""Exchange calendars (NYSE unchanged, TSX holidays / in-lieu days / early close) and symbol -> market keying."""
from datetime import date, datetime, time

import pytest

from optionlens.analytics import calendar as cal
from optionlens.analytics import markets
from optionlens.analytics.calendar import NY, NYSE, TSX


# tsx.com "Calendars & Trading Hours" (read 2026-11-24), the 2026 and 2027 lists
TSX_2026 = {date(2026, 1, 1), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 18), date(2026, 7, 1),
            date(2026, 8, 3), date(2026, 9, 7), date(2026, 10, 12), date(2026, 12, 25), date(2026, 12, 28)}
TSX_2027 = {date(2027, 1, 1), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 24), date(2027, 7, 1),
            date(2027, 8, 2), date(2027, 9, 6), date(2027, 10, 11), date(2027, 12, 27), date(2027, 12, 28)}


def test_tsx_holidays_match_the_published_lists():
    assert TSX.holidays(2026) == TSX_2026
    assert TSX.holidays(2027) == TSX_2027


def test_tsx_weekend_holidays_move_to_the_next_free_weekday():
    # 2022: New Year's Day on Saturday -> Monday Jan 3; Christmas on Sunday -> Monday Dec 26, Boxing Day -> Tuesday
    h22 = TSX.holidays(2022)
    assert {date(2022, 1, 3), date(2022, 12, 26), date(2022, 12, 27)} <= h22
    assert date(2021, 12, 31) not in TSX.holidays(2021)      # never the Friday before (unlike NYSE's Saturday rule)
    # 2023: Canada Day on Saturday -> Monday July 3
    assert date(2023, 7, 3) in TSX.holidays(2023)
    # 2026: Christmas Friday, Boxing Day Saturday -> Monday Dec 28
    assert TSX.is_trading_day(date(2026, 12, 24)) and not TSX.is_trading_day(date(2026, 12, 28))


def test_tsx_victoria_day_is_the_monday_before_may_25():
    assert [d for d in TSX.holidays(2025) if d.month == 5] == [date(2025, 5, 19)]
    assert [d for d in TSX.holidays(2027) if d.month == 5] == [date(2027, 5, 24)]   # May 24 itself is a Monday


def test_tsx_early_close_christmas_eve_only():
    assert TSX.close_time(date(2026, 12, 24)) == time(13, 0)
    assert TSX.close_time(date(2027, 12, 24)) == time(13, 0)
    assert TSX.close_time(date(2026, 12, 31)) == time(16, 0)
    assert TSX.close_time(date(2026, 11, 27)) == time(16, 0)    # the US day after Thanksgiving is a full TSX day
    assert TSX.early_closes(2028) == frozenset()                 # Dec 24 2028 is a Sunday


def test_us_only_holidays_are_tsx_trading_days_and_vice_versa():
    for d in (date(2026, 1, 19), date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 11, 26)):
        assert TSX.is_trading_day(d) and not NYSE.is_trading_day(d)
    for d in (date(2026, 5, 18), date(2026, 7, 1), date(2026, 8, 3), date(2026, 10, 12), date(2026, 12, 28)):
        assert NYSE.is_trading_day(d) and not TSX.is_trading_day(d)
    assert cal.any_trading_day(date(2026, 7, 3)) and cal.any_trading_day(date(2026, 7, 1))
    assert not cal.any_trading_day(date(2026, 4, 3))             # Good Friday: both closed
    assert cal.latest_close_time(date(2026, 11, 27)) == time(16, 0)   # NYSE 13:00, TSX 16:00


def test_nyse_module_functions_are_unchanged():
    assert cal.is_trading_day == NYSE.is_trading_day
    assert date(2026, 11, 26) in cal.holidays(2026)
    assert cal.close_time(date(2026, 11, 27)) == time(13, 0)
    assert cal.last_completed_session(datetime(2026, 11, 27, 13, 5, tzinfo=NY)) == date(2026, 11, 27)


def test_session_arithmetic_per_exchange():
    # Victoria Day Monday 2026-05-18: NYSE open, TSX closed
    ts = datetime(2026, 5, 18, 11, 0, tzinfo=NY)
    assert NYSE.is_market_open(ts) and not TSX.is_market_open(ts)
    assert TSX.last_completed_session(ts) == date(2026, 5, 15)
    assert TSX.next_trading_day(date(2026, 5, 15)) == date(2026, 5, 19)
    assert TSX.add_trading_days(date(2026, 12, 23), 2) == date(2026, 12, 29)


def test_monthly_expiry_moves_before_a_good_friday_on_each_exchange():
    # 2025-04-18 was Good Friday (third Friday of April): both exchanges expire Thursday 04-17
    assert TSX.is_monthly_expiry(date(2025, 4, 17)) and NYSE.is_monthly_expiry(date(2025, 4, 17))
    assert TSX.is_monthly_expiry(date(2026, 12, 18))


@pytest.mark.parametrize("symbol,market,currency,exchange,ticker", [
    ("RY", "US", "USD", None, "RY"),
    ("RY.TO", "CA", "CAD", "TSX", "RY"),
    ("RCI.B.TO", "CA", "CAD", "TSX", "RCI.B"),
    ("AP.UN.TO", "CA", "CAD", "TSX", "AP.UN"),
    ("ABC.VN", "CA", "CAD", "TSXV", "ABC"),
    ("BRK.B", "US", "USD", None, "BRK.B"),
    ("TO", "US", "USD", None, "TO"),      # a bare ticker that merely looks like a suffix
])
def test_symbol_keying(symbol, market, currency, exchange, ticker):
    assert markets.market_of(symbol).code == market
    assert markets.currency_of(symbol) == currency
    assert markets.listing_exchange(symbol) == exchange
    assert markets.ticker(symbol) == ticker
    assert markets.calendar_for(symbol) is (TSX if market == "CA" else NYSE)


def test_us_and_canadian_listings_of_one_company_are_distinct_symbols():
    us, ca = "RY", markets.canadian_symbol("ry", "TSX")
    assert ca == "RY.TO" and us != ca
    assert markets.market_of(us) is markets.US and markets.market_of(ca) is markets.CA
    assert markets.canadian_symbol("XYZ", "TSXV") == "XYZ.VN"
