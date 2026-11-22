"""Which chain is a session's chain when several sources deliver one (docs/INTEGRATION.md §6 "Chain precedence").

Applies within one tier of a session's metrics_daily row (the session's own data, or after-hours fills: see
jobs/chains.py) and to its end-of-day archive (option_eod). A chain from the source that stored the current one
always replaces it (newer data of the same source). A chain from another source replaces it only when it is
better, compared in this order:
1. a closing snapshot (`final`) beats an intraday chain;
2. real-time data beats delayed (or unknown) data;
3. more quoted contracts (`quoted`: an ask > 0 and some bid, OI or volume, i.e. what the archive keeps) wins;
4. equal on all three: the source listed first in settings.chain_sources (default IBKR, then Questrade).
option_latest / quotes_latest are not ranked: they always hold the newest chain. Reconstructed archives (from
option bars) are below every real chain (repo.options).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..models import ChainSnapshot, OptionContract
from ..repo import options as options_repo
from ..sources import chain_source


@dataclass(frozen=True)
class Quality:
    source: str | None
    final: bool
    realtime: bool
    quoted: int


def quoted(contracts: Iterable[OptionContract]) -> int:
    """Contracts with a real ask that the archive would keep (repo.options.archivable)."""
    return sum(1 for c in contracts if c.ask is not None and c.ask > 0 and options_repo.archivable(c))


def of_snapshot(snap: ChainSnapshot, source: str | None, final: bool) -> Quality:
    return Quality(source, final, snap.delayed is False, quoted(snap.contracts))


def of_metrics_row(row: Mapping) -> Quality:
    """The quality of the chain behind a stored metrics_daily row (rows from before these columns: unknown,
    i.e. intraday, not real-time, nothing quoted)."""
    return Quality(row.get("source"), bool(row.get("chain_final")), row.get("delayed") is False,
                   int(row.get("chain_quoted") or 0))


def of_archive(a: Mapping) -> Quality:
    """repo.options.eod_quality entry -> Quality (archives are closing snapshots)."""
    return Quality(a["source"], True, a["delayed"] is False, int(a["quoted"]))


def _rank(source: str | None, order: Sequence[str]) -> int:
    return order.index(source) if source in order else len(order)


def replaces(existing: Quality | None, new: Quality, order: Sequence[str] | None = None) -> bool:
    """Whether `new` replaces the stored chain `existing` (see module doc)."""
    if existing is None or existing.source == new.source:
        return True
    order = chain_source.preference() if order is None else order

    def key(q: Quality) -> tuple:
        return q.final, q.realtime, q.quoted, -_rank(q.source, order)
    return key(new) > key(existing)
