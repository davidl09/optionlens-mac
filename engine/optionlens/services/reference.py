"""Reference data from the server-side sources (docs/INTEGRATION.md §8 "Reference data"): what a source already fetched
for its chains today (Questrade GET v1/symbols/:id, once per symbol per New York day) is stored without another call —
one fundamentals_snapshot per symbol per day and the current dividend (source 'questrade') into the dividends model.

`store_reference` runs after every sweep (jobs.hooks.on_sweep_complete; register_hooks) and stores the symbols not
stored yet that day.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Mapping, Protocol

from ..analytics.calendar import now_ny
from ..jobs import hooks
from ..models import Dividend, Fundamentals
from ..repo import dividends as div_repo
from ..repo import fundamentals as fund_repo
from ..sources import chain_source

Reference = Mapping[str, tuple[Fundamentals, Dividend | None]]


class ReferenceSource(Protocol):
    name: str

    def cached_reference(self, day: date) -> Reference: ...


def _sources() -> list[ReferenceSource]:
    return [s for s in chain_source.server_sources() if hasattr(s, "cached_reference")]


def store(ref: Reference, day: date, now: datetime | None = None) -> int:
    """Store the snapshots and current dividends of `ref` (fetched on `day`) not stored yet for that day. Returns
    snapshots written."""
    done = fund_repo.stored_on(day)
    todo = {s: r for s, r in ref.items() if s not in done}
    if not todo:
        return 0
    now = now or datetime.now(timezone.utc)
    div_repo.upsert_derived([d for _, d in todo.values() if d is not None], now)
    return fund_repo.upsert([f for f, _ in todo.values()], now)


def store_reference(_day: date | None = None, _symbols: list[str] | None = None) -> int:
    """on_sweep_complete hook: every source's reference data cached today (New York) -> storage. (The hook's `day` is
    the session's, which differs from the fetch day on weekends and before the open.)"""
    day = now_ny().date()
    return sum(store(src.cached_reference(day), day) for src in _sources())


def register_hooks() -> None:
    """Store reference data after every sweep (idempotent; call once at startup)."""
    if store_reference not in hooks.on_sweep_complete:
        hooks.on_sweep_complete.append(store_reference)
