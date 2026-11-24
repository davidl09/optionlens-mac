"""What the renderer windows show (docs/INTEGRATION.md §9), per client, and their union: exactly what the engine keeps
live. Pure: chain views arrive already resolved to contracts (live/resolve.py)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .session import Session


@dataclass(frozen=True)
class Wanted:
    """Underlying symbols and option contracts (OSI -> underlying symbol) to keep live."""
    symbols: frozenset[str] = frozenset()
    contracts: Mapping[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.symbols and not self.contracts

    def keys(self) -> tuple[frozenset[str], frozenset[str]]:
        return self.symbols, frozenset(self.contracts)


EMPTY = Wanted()


@dataclass(frozen=True)
class Diff:
    added_symbols: frozenset[str]
    removed_symbols: frozenset[str]
    added_contracts: frozenset[str]
    removed_contracts: frozenset[str]

    @property
    def empty(self) -> bool:
        return not (self.added_symbols or self.removed_symbols or self.added_contracts or self.removed_contracts)


def union(wanted: Iterable[Wanted]) -> Wanted:
    symbols: set[str] = set()
    contracts: dict[str, str] = {}
    for w in wanted:
        symbols |= w.symbols
        contracts.update(w.contracts)
    return Wanted(frozenset(symbols), contracts)


def diff(old: Wanted, new: Wanted) -> Diff:
    oc, nc = frozenset(old.contracts), frozenset(new.contracts)
    return Diff(new.symbols - old.symbols, old.symbols - new.symbols, nc - oc, oc - nc)


def for_session(w: Wanted, session: Session) -> Wanted:
    """What can be live in `session`: everything in the regular session, underlyings only in extended hours (US
    options trade 09:30-16:00), nothing while the market is closed."""
    if session == "regular":
        return w
    if session == "extended":
        return Wanted(w.symbols)
    return EMPTY


class Registry:
    """The latest subscription of each client (a renderer window); a new one replaces the client's previous set."""

    def __init__(self) -> None:
        self._clients: dict[str, Wanted] = {}
        self._union = EMPTY

    def set(self, client_id: str, w: Wanted) -> bool:
        """Replace the client's set. Returns whether the union changed."""
        self._clients[client_id] = w
        return self._recompute()

    def drop(self, client_id: str) -> bool:
        """Forget the client. Returns whether the union changed."""
        if self._clients.pop(client_id, None) is None:
            return False
        return self._recompute()

    def get(self, client_id: str) -> Wanted:
        return self._clients.get(client_id, EMPTY)

    def clients(self) -> list[str]:
        return list(self._clients)

    def union(self) -> Wanted:
        return self._union

    def _recompute(self) -> bool:
        new = union(self._clients.values())
        changed = not diff(self._union, new).empty
        self._union = new
        return changed
