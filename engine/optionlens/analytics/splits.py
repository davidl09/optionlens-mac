"""Split adjustment of daily bars: whether stored bars around a split's ex-date still show the unadjusted price
jump (then the bars before it are adjusted), so an already adjusted history is never adjusted twice."""
from __future__ import annotations

import math

MIN_FACTOR_LOG = math.log(1.25)    # splits/stock dividends closer to 1:1 than 5:4 cannot be told from a day's move
MAX_ADJUSTED_MOVE_LOG = math.log(1.5)   # after adjustment, the ex-date move must look like an ordinary day


def shows_unadjusted_split(close_before: float, close_on: float, factor: float | None) -> bool:
    """True when the close on/after a split's ex-date vs the close before it shows the split's jump (close_on ≈
    close_before / factor, factor = new shares per old share: 2 for 2:1, 0.05 for 1:20), i.e. the bars before the
    ex-date are not split-adjusted yet. False for adjusted data, unknown factors or prices, or factors near 1."""
    if not factor or factor <= 0 or abs(math.log(factor)) < MIN_FACTOR_LOG:
        return False
    if not (close_before and close_before > 0 and close_on and close_on > 0):
        return False
    raw = math.log(close_on / close_before)
    adjusted = math.log(close_on * factor / close_before)
    return abs(adjusted) < abs(raw) and abs(adjusted) < MAX_ADJUSTED_MOVE_LOG
