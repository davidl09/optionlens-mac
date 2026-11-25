"""Earnings event variance from the ATM term structure and the per-leg IV crush (analytics.event_vol)."""
from datetime import date, datetime

import numpy as np
import pytest

from optionlens.analytics import event_vol as ev
from optionlens.analytics import payoff
from optionlens.analytics.calendar import NY, year_fraction_to_expiry
from optionlens.analytics.strategies import Leg, value_at

NOW = datetime(2026, 11, 25, 16, 0, tzinfo=NY)
REACT = date(2026, 12, 3)            # CRM reports 2026-12-02 after the close
FRONT, BACK = date(2026, 12, 4), date(2026, 12, 24)


def row(expiry: date, iv: float | None) -> dict:
    return {"expiry": expiry, "dte": (expiry - NOW.date()).days, "t": year_fraction_to_expiry(expiry, NOW), "atm_iv": iv}


CRM = [row(date(2026, 11, 27), 0.55), row(FRONT, 0.917), row(BACK, 0.62)]


def test_post_event_pair_on_the_crm_numbers():
    e, why = ev.estimate(CRM, REACT)
    assert why is None and e.method == "post_event_pair"          # the 2-day pre-event expiry is too short
    assert e.expiries == (FRONT, BACK) and e.reaction == REACT
    assert e.base_iv == pytest.approx(0.4231, abs=5e-4)
    assert e.move == pytest.approx(0.1278, abs=5e-4)
    t1, t2 = 9 / 365, 29 / 365
    assert 0.917 ** 2 * t1 == pytest.approx(e.base_iv ** 2 * t1 + e.variance)    # both expiries are reproduced
    assert 0.62 ** 2 * t2 == pytest.approx(e.base_iv ** 2 * t2 + e.variance)


def test_pre_event_expiry_gives_the_base():
    pre = row(date(2026, 12, 2), 0.40)                           # 7 days: long enough
    e, _ = ev.estimate([pre, row(FRONT, 0.917), row(BACK, 0.62)], REACT)
    assert e.method == "pre_event" and e.expiries == (date(2026, 12, 2), FRONT)
    assert e.base_iv == 0.40
    assert e.variance == pytest.approx((0.917 ** 2 - 0.40 ** 2) * 9 / 365)


def test_second_post_event_expiry_must_be_a_week_later():
    e, _ = ev.estimate([row(FRONT, 0.917), row(date(2026, 12, 8), 0.8), row(BACK, 0.62)], REACT)
    assert e.expiries == (FRONT, BACK)


@pytest.mark.parametrize("rows,reason", [
    ([row(date(2026, 11, 27), 0.5)], "no expiry"),                                      # nothing after the event
    ([row(FRONT, 0.917)], "no second post-event expiry"),
    ([row(FRONT, 0.40), row(BACK, 0.60)], "no earnings premium"),                         # rising term structure: J <= 0
    ([row(FRONT, 0.90), row(date(2026, 12, 11), 0.30)], "no positive base variance"),   # negative forward variance
    ([row(date(2026, 12, 2), 0.50), row(FRONT, 0.45)], "no earnings premium"),          # pre-event above post-event
    ([row(FRONT, None), row(BACK, 0.62)], "no second post-event expiry"),               # missing IV ignored
])
def test_no_estimate_says_why(rows, reason):
    e, why = ev.estimate(rows, REACT)
    assert e is None and reason in why


def test_leg_split_and_floor():
    e, _ = ev.estimate(CRM, REACT)
    s = ev.split_leg(0.62, 29 / 365, e)
    assert s.base == pytest.approx(e.base_iv) and s.jump == e.variance and not s.floored
    low = ev.split_leg(0.55, 9 / 365, e)                          # a wing of the front expiry far below its ATM IV
    assert low.floored and low.base == pytest.approx(ev.BASE_FLOOR * e.base_iv)
    assert low.base ** 2 + low.jump / (9 / 365) == pytest.approx(0.55 ** 2)   # still prices at its own IV today
    tiny = ev.split_leg(0.15, 9 / 365, e)                         # below the floor itself: no jump at all
    assert tiny.base == 0.15 and tiny.jump == 0.0


def test_crush_vol_through_time():
    e, _ = ev.estimate(CRM, REACT)
    back, front = Leg("option", 1, "C", 200, BACK, 11.0, 0.62), Leg("option", -1, "C", 200, FRONT, 9.0, 0.917)
    early = Leg("option", 1, "P", 190, date(2026, 11, 27), 1.0, 0.55)            # expires before the event
    vol = ev.CrushVol([back, front, early], e, NOW)
    assert vol(back, NOW) == pytest.approx(0.62) and vol(front, NOW) == pytest.approx(0.917)
    before = datetime(2026, 12, 2, 16, 0, tzinfo=NY)
    assert 0.62 < vol(back, before) < vol(front, before)            # the jump is concentrated as time passes
    after = datetime(2026, 12, 3, 9, 30, tzinfo=NY)
    assert vol(back, after) == pytest.approx(e.base_iv) and vol(front, after) == pytest.approx(e.base_iv)
    assert vol(early, NOW) == 0.55 and vol.split(early) is None
    late = ev.CrushVol([back], e, datetime(2026, 12, 3, 10, 0, tzinfo=NY))     # event already behind now
    assert late.split(back) is None and late(back, after) == 0.62


def test_no_event_in_any_leg_gives_identical_results():
    legs = [Leg("option", -1, "C", 200, FRONT, 9.0, 0.917), Leg("option", 1, "C", 200, BACK, 11.0, 0.62)]
    later, _ = ev.estimate([row(date(2027, 1, 15), 0.5), row(date(2027, 2, 19), 0.7), row(date(2027, 3, 19), 0.5)],
                           date(2027, 1, 29))
    vol = ev.CrushVol(legs, later, NOW)                             # an event after both expiries
    S = np.linspace(150, 250, 51)
    at = datetime(2026, 12, 4, 16, 0, tzinfo=NY)
    assert np.array_equal(value_at(legs, S, at, 0.04, 0.0, 0.02, vol), value_at(legs, S, at, 0.04, 0.0, 0.02))
    a = payoff.calculate(legs, 194.0, NOW, 0.04, 0.0, hv30=0.35, iv_shift=0.02, vol=vol)
    b = payoff.calculate(legs, 194.0, NOW, 0.04, 0.0, hv30=0.35, iv_shift=0.02)
    assert a == b
