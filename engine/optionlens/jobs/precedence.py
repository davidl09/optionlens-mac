"""Which chain wins when several sources deliver one (docs/INTEGRATION.md §6 "Chain precedence"). One rule for the
served chain (option_latest), a session's metrics_daily row (within one tier: the session's own data, or after-hours
fills: see jobs/chains.py) and its end-of-day archive (option_eod). No source is preferred over another.

A chain of a later session replaces one of an earlier session. Within one session:
- two non-final chains fetched more than SAME_SNAPSHOT apart are not the same snapshot: the fresher one wins;
- otherwise they are compared in this order:
  1. a closing snapshot (`final`) beats a chain that is not one;
  2. real-time data beats delayed (or unknown) data;
  3. more quoted contracts (`quoted`: an ask > 0 and some bid, OI or volume, i.e. what the archive keeps) wins;
  4. the fresher fetch wins.
The same chain stored again (same source and fetch time, e.g. after it took another source's IVs) replaces itself.
Reconstructed archives (from option bars) are below every real chain (repo.options).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping

from ..models import ChainSnapshot, OptionContract
from ..repo import options as options_repo
from ._util import session_date

SAME_SNAPSHOT = timedelta(minutes=15)
_UNKNOWN_TIME = datetime.min.replace(tzinfo=timezone.utc)   # rows from before fetch times were kept: oldest


@dataclass(frozen=True)
class Quality:
    source: str | None
    final: bool
    realtime: bool
    quoted: int
    fetched_at: datetime | None = None   # None: unknown (older rows)
    session: date | None = None          # None: the caller compares chains of one session


def quoted(contracts: Iterable[OptionContract]) -> int:
    """Contracts with a real ask that the archive would keep (repo.options.archivable)."""
    return sum(1 for c in contracts if c.ask is not None and c.ask > 0 and options_repo.archivable(c))


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def of_snapshot(snap: ChainSnapshot, source: str | None, final: bool) -> Quality:
    return Quality(source, final, snap.delayed is False, quoted(snap.contracts), snap.fetched_at,
                   session_date(snap.fetched_at))


def of_metrics_row(row: Mapping) -> Quality:
    """The quality of the chain behind a stored metrics_daily row (rows from before these columns: unknown,
    i.e. intraday, not real-time, nothing quoted, fetched long ago)."""
    return Quality(row.get("source"), bool(row.get("chain_final")), row.get("delayed") is False,
                   int(row.get("chain_quoted") or 0), _aware(row.get("chain_fetched_at")))


def of_archive(a: Mapping) -> Quality:
    """repo.options.eod_quality entry -> Quality (archives are closing snapshots)."""
    return Quality(a["source"], True, a["delayed"] is False, int(a["quoted"]), _aware(a.get("fetched_at")))


def of_latest(a: Mapping | None) -> Quality | None:
    """repo.options.latest_quality entry -> Quality of the served chain (None: none stored)."""
    if a is None:
        return None
    fetched = _aware(a["fetched_at"])
    return Quality(a["source"], bool(a["final"]), a["delayed"] is False, int(a["quoted"]), fetched,
                   session_date(fetched) if fetched else None)


def replaces(existing: Quality | None, new: Quality) -> bool:
    """Whether `new` replaces the stored chain `existing` (see module doc)."""
    if existing is None:
        return True
    if existing.session is not None and new.session is not None and existing.session != new.session:
        return new.session > existing.session
    old_t, new_t = existing.fetched_at or _UNKNOWN_TIME, new.fetched_at or _UNKNOWN_TIME
    if existing.source == new.source and old_t == new_t:
        return True
    if not existing.final and not new.final and abs(new_t - old_t) > SAME_SNAPSHOT:
        return new_t > old_t
    return (new.final, new.realtime, new.quoted, new_t) > (existing.final, existing.realtime, existing.quoted, old_t)
