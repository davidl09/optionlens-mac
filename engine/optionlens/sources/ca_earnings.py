"""Next earnings dates of Canadian listings (Nasdaq covers US listings only): per symbol, the first source in
PRECEDENCE with a date — TMX Money, then Yahoo Finance. US listings never go through these sources.

Both are unofficial endpoints that may break. A source that fails (transport error, throttling, a changed answer) is
unavailable for the rest of the run: what it answered before stays, and the symbols left are asked of the next
source. A Selection records who answered each symbol, each source's health and, per unanswered symbol, why.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable, Mapping, Sequence

from ..models import EarningsEvent
from . import tmx, yahoo

PRECEDENCE = (tmx.NAME, yahoo.NAME)
LABELS = {tmx.NAME: "TMX Money", yahoo.NAME: "Yahoo Finance"}

# (symbols, today) -> ({symbol: next event}, None) or (answered so far, why the source failed)
Fetch = Callable[[Sequence[str], date], Awaitable[tuple[dict[str, EarningsEvent], "str | None"]]]
FETCHERS: dict[str, Fetch] = {tmx.NAME: lambda s, d: tmx.fetch_upcoming(s, d),
                              yahoo.NAME: lambda s, d: yahoo.fetch_upcoming(s, d)}


@dataclass(frozen=True)
class Selection:
    events: dict[str, EarningsEvent]   # symbol -> next event (event.source: who answered)
    health: dict[str, str | None]      # source -> None (answered) | why it was unavailable; absent: not needed
    unanswered: tuple[str, ...]        # symbols no source had a date for

    def counts(self) -> dict[str, int]:
        """{source: symbols it answered, "none": unanswered} (every source in PRECEDENCE listed)."""
        out = {s: 0 for s in PRECEDENCE}
        for ev in self.events.values():
            out[ev.source or "?"] = out.get(ev.source or "?", 0) + 1
        return {**out, "none": len(self.unanswered)}

    def why_none(self) -> str:
        """Why an unanswered symbol has no date, from the asked sources' health: "TMX Money and Yahoo Finance were
        unavailable (…)" when none could be reached, else each one's outcome ("TMX Money has no upcoming date; …")."""
        asked = [s for s in PRECEDENCE if s in self.health]
        if not asked:
            return "no source was asked"
        failed = [s for s in asked if self.health[s] is not None]
        reasons = "; ".join(f"{LABELS[s]}: {self.health[s]}" for s in failed)
        if len(failed) == len(asked):
            return f"{' and '.join(LABELS[s] for s in asked)} {'were' if len(asked) > 1 else 'was'} unavailable ({reasons})"
        return "; ".join(f"{LABELS[s]} has no upcoming date" if self.health[s] is None
                         else f"{LABELS[s]} was unavailable ({self.health[s]})" for s in asked)


async def select_upcoming(symbols: Sequence[str], today: date, down: Mapping[str, str] | None = None,
                          fetchers: Mapping[str, Fetch] | None = None) -> Selection:
    """Each symbol's next earnings from the first source (PRECEDENCE) that has one. `down`: sources already known
    to be unavailable this run (source -> reason; they are not asked). `fetchers` replaces FETCHERS (tests)."""
    fetch = {**FETCHERS, **(fetchers or {})}
    pending = list(dict.fromkeys(symbols))
    events: dict[str, EarningsEvent] = {}
    health: dict[str, str | None] = {}
    for name in PRECEDENCE:
        if not pending:
            break
        if down and name in down:
            health[name] = down[name]
            continue
        got, err = await fetch[name](pending, today)
        health[name] = err
        events.update({s: ev for s, ev in got.items() if s in pending and ev.source == name})
        pending = [s for s in pending if s not in events]
    return Selection(events, health, tuple(pending))
