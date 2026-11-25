"""Earnings event volatility for the strategy calculator: split implied variance into a diffusive base and an
event jump, and value option legs through the event (the IV crush). Pure functions, no I/O.

Model
-----
An expiry that includes the earnings reaction session carries total implied variance σ²·T = b²·T + J, with b the
ex-event ("base") volatility and J the event's total variance (annualised variance × years, i.e. the variance of the
reaction-day log move; sqrt(J) is the implied move). Year fractions are calendar time to 16:00 ET on expiry
(calendar.year_fraction_to_expiry), as everywhere in the engine. The jump happens at the open (09:30 ET) of the
reaction session: ChainArrays.next_reaction (timing-aware: an after-close report reacts the next session), so an
expiry on or after the reaction day includes the event (ChainArrays.earnings_before_expiry).

Estimating J from the ATM term structure (vol.atm_iv_by_expiry rows: expiry, dte, t, atm_iv; DTE >= 1 only)
* "pre_event" (preferred): σ0 = ATM IV of the last expiry before the reaction session, usable when it has at least
  MIN_BASE_DTE calendar days left; σ1, T1 = the first expiry on/after the reaction session: J = (σ1² − σ0²)·T1,
  base = σ0.
* "post_event_pair" (no usable pre-event expiry): σ1, T1 the first post-event expiry and σ2, T2 the next one at least
  MIN_PAIR_GAP_DAYS later: b² = (σ2²·T2 − σ1²·T1)/(T2 − T1) (the forward variance between them, event-free),
  J = σ1²·T1 − b²·T1.
* J <= 0, a negative forward variance, or missing IVs: no estimate (the reason says which).

MIN_BASE_DTE = 7: an expiry with under a week left spans no full weekly cycle, so its calendar-day IV is dominated
by the weekend / holiday days it counts as trading time and by quote noise on a few cents of premium — too unreliable
to anchor the base.

Leg vols
--------
A leg whose life contains the event (now < event instant <= its 16:00 ET expiry) with IV σ and T = now → expiry is
split into its own base b_leg² = σ² − J/T. Valued at time t with τ = t → expiry: σ(t) = sqrt(b_leg² + J/τ) while the
event is still ahead of t, b_leg once it is behind. So σ(now) = σ exactly (today's P/L is unchanged) and the jump's
variance is spent at the event, not spread over the leg's life. Legs expiring before the event, and stock, are
unchanged.
Floor: b_leg >= BASE_FLOOR × the chain's base IV (and <= σ). An off-ATM leg whose IV sits well below the ATM IV
(skew) would otherwise get a tiny or negative base; when the floor binds, that leg's jump is reduced to
(σ² − b_leg²)·T so σ(now) still equals σ, and the leg reports base_floored.
iv_shift is added to σ(t) by the caller (strategies.leg_sigma): the slider keeps meaning "vol points added to every
leg's IV at every date".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence

from .calendar import NY, OPEN, year_fraction_to_expiry
from .strategies import Leg, close_dt

MIN_BASE_DTE = 7          # calendar days a pre-event expiry needs to give the base IV
MIN_PAIR_GAP_DAYS = 7     # post_event_pair: T2 at least this many calendar days after T1
BASE_FLOOR = 0.5          # a leg's base IV is at least this fraction of the chain's base IV


def event_instant(reaction: date) -> datetime:
    """When the earnings jump happens: the open of the reaction session."""
    return datetime.combine(reaction, OPEN, NY)


def event_ahead(reaction: date | None, now: datetime, until: date) -> bool:
    """The reaction session opens after ``now`` and on or before the 16:00 ET close of ``until``."""
    return reaction is not None and now < event_instant(reaction) <= close_dt(until)


@dataclass(frozen=True)
class EventVol:
    reaction: date
    variance: float              # J: total variance of the event (annualised variance × years)
    base_iv: float               # the chain's ex-event ATM IV
    method: str                  # "pre_event" | "post_event_pair"
    expiries: tuple[date, date]  # the two expiries the estimate came from

    @property
    def at(self) -> datetime:
        return event_instant(self.reaction)

    @property
    def move(self) -> float:
        """Implied event move (one standard deviation of the reaction-day log return)."""
        return math.sqrt(self.variance)


def estimate(term: Sequence[Mapping], reaction: date) -> tuple[EventVol | None, str | None]:
    """(EventVol, None) or (None, why not) from ATM term-structure rows {expiry, dte, t, atm_iv}."""
    rows = sorted((r for r in term if r.get("atm_iv") is not None and r.get("dte", 0) >= 1 and r.get("t", 0) > 0),
                  key=lambda r: r["expiry"])
    post = [r for r in rows if r["expiry"] >= reaction]
    if not post:
        return None, "no expiry with an ATM IV on or after the earnings reaction"
    p1 = post[0]
    s1, t1 = float(p1["atm_iv"]), float(p1["t"])
    pre = [r for r in rows if r["expiry"] < reaction]
    if pre and pre[-1]["dte"] >= MIN_BASE_DTE:
        p0 = pre[-1]
        base = float(p0["atm_iv"])
        J = (s1 * s1 - base * base) * t1
        method, used = "pre_event", (p0["expiry"], p1["expiry"])
    else:
        p2 = next((r for r in post[1:] if (r["expiry"] - p1["expiry"]).days >= MIN_PAIR_GAP_DAYS), None)
        if p2 is None:
            return None, (f"no usable pre-event expiry (needs {MIN_BASE_DTE}+ days) and no second post-event expiry "
                          f"{MIN_PAIR_GAP_DAYS}+ days after {p1['expiry']}")
        s2, t2 = float(p2["atm_iv"]), float(p2["t"])
        fwd = (s2 * s2 * t2 - s1 * s1 * t1) / (t2 - t1)
        if fwd <= 0:
            return None, f"the term structure gives no positive base variance between {p1['expiry']} and {p2['expiry']}"
        base = math.sqrt(fwd)
        J = (s1 * s1 - fwd) * t1
        method, used = "post_event_pair", (p1["expiry"], p2["expiry"])
    if not J > 0:
        return None, f"the term structure prices no earnings premium ({used[0]} vs {used[1]})"
    return EventVol(reaction, float(J), float(base), method, used), None


@dataclass(frozen=True)
class LegSplit:
    base: float        # the leg's ex-event IV
    jump: float        # the leg's event variance (= J unless the floor bound)
    floored: bool


def split_leg(iv: float, T: float, ev: EventVol) -> LegSplit:
    """A leg's base IV and event variance: b² = iv² − J/T, floored at BASE_FLOOR × the chain's base (<= iv)."""
    floor = min(iv, BASE_FLOOR * ev.base_iv)
    b2 = iv * iv - ev.variance / T
    if b2 >= floor * floor:
        return LegSplit(math.sqrt(b2), ev.variance, False)
    return LegSplit(floor, max(iv * iv - floor * floor, 0.0) * T, True)


class CrushVol:
    """strategies.LegVol with the earnings crush: a leg's IV at time t (before iv_shift), per the module docstring."""

    def __init__(self, legs: Sequence[Leg], ev: EventVol, now: datetime):
        self.event = ev
        self._split: dict[Leg, LegSplit] = {}
        for l in legs:
            if l.kind == "option" and l.iv is not None and event_ahead(ev.reaction, now, l.expiry):
                self._split[l] = split_leg(l.iv, year_fraction_to_expiry(l.expiry, now), ev)

    def split(self, leg: Leg) -> LegSplit | None:
        """The leg's base / jump, None when the event is not inside its life."""
        return self._split.get(leg)

    def __call__(self, leg: Leg, at: datetime) -> float:
        s = self._split.get(leg)
        if s is None:
            return leg.iv  # type: ignore[return-value]
        if at >= self.event.at:
            return s.base
        tau = year_fraction_to_expiry(leg.expiry, at)  # type: ignore[arg-type]
        return math.sqrt(s.base * s.base + s.jump / tau)
