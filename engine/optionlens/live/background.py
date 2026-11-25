"""The background set (docs/INTEGRATION.md §9 "Background symbols"): the symbols the user follows, kept live by the
Questrade driver while no window shows them so that their 1-minute bars (live/bars.py) are already built when a chart
opens (the candle source answers the latest ~15 minutes late). Pure: the hub gets the lists from an injected reader.

- priority: portfolio holdings' underlyings (largest first), then watchlist symbols, then the RECENT_MAX symbols
  opened most recently within RECENT_WINDOW; duplicates count once, at their first place;
- at most BACKGROUND_MAX symbols in all (one Level 1 equity quote each: well inside Questrade's streaming and polling
  limits; one stream carries them with the windows' keys);
- underlyings only (no option contracts); the hub applies each symbol's market session (live/subscriptions.py).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable

BACKGROUND_MAX = 40
RECENT_MAX = 15
RECENT_WINDOW = timedelta(days=3)
REFRESH_S = 60.0          # how often the hub re-reads the lists


def compose(portfolio: Iterable[str], watchlists: Iterable[str], recent: Iterable[str],
            cap: int = BACKGROUND_MAX) -> tuple[str, ...]:
    """The background set in priority order: portfolio, watchlists, the first RECENT_MAX of `recent` (most recent
    first), at most `cap` symbols."""
    recent_first = list(dict.fromkeys(recent))[:RECENT_MAX]
    return tuple(dict.fromkeys([*portfolio, *watchlists, *recent_first]))[:cap]
