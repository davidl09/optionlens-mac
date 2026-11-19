"""Which earnings event is still "next" on the symbol page (reaction session not started)."""
from datetime import date, datetime

from optionlens.analytics.calendar import NY
from optionlens.models import EarningsEvent
from optionlens.services.symbol_earnings import is_pending


def ev(d, timing):
    return EarningsEvent("X", d, timing, None, None, None, None, None)


def test_bmo_today_is_past_once_the_session_opened():
    today = date(2026, 11, 19)
    assert is_pending(ev(today, "bmo"), datetime(2026, 11, 19, 8, 0, tzinfo=NY))
    assert not is_pending(ev(today, "bmo"), datetime(2026, 11, 19, 10, 0, tzinfo=NY))   # reaction under way
    assert is_pending(ev(today, "amc"), datetime(2026, 11, 19, 15, 0, tzinfo=NY))       # reacts tomorrow
    assert is_pending(ev(today, "unknown"), datetime(2026, 11, 19, 15, 0, tzinfo=NY))   # treated as amc
    assert not is_pending(ev(date(2026, 11, 18), "amc"), datetime(2026, 11, 19, 10, 0, tzinfo=NY))
