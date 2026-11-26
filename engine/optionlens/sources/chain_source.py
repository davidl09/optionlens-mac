"""Option-chain sources. `settings.chain_sources` (OPTIONLENS_CHAIN_SOURCES) lists the enabled ones:
- "ibkr-bridge": chains pushed by the app's IBKR bridge through the ingest API (rows tagged "ibkr"), for every
  market OptionLens follows (US listings; Canadian ones with their Montréal Exchange options, docs/INTEGRATION.md §6);
- any other name: a server-side ChainSource from REGISTRY, which the scheduler sweeps itself while the source is
  available (rows tagged with its name, e.g. "questrade").
No source is preferred: which chain wins is decided by quality and freshness alone (jobs/precedence.py).
"""
from __future__ import annotations

from typing import Callable, Protocol

from ..config import get_settings
from ..models import ChainSnapshot

BRIDGE = "ibkr-bridge"
BRIDGE_TAG = "ibkr"


class NotListed(Exception):
    """The source does not carry this symbol: nothing is stored and the symbol is not marked unoptionable."""


class ChainSource(Protocol):
    name: str

    def connected(self) -> bool:
        """Whether the user set the source up (e.g. pasted a token), whether or not it can fetch now."""
        ...

    def unavailable(self) -> str | None:
        """Why the source cannot fetch now (e.g. "not connected", a permission problem), or None."""
        ...

    def problem(self) -> str | None:
        """While unavailable for a reason the user must fix: its short form (e.g. "403 code 1016 (missing
        market-data permission)"), else None."""
        ...

    def status(self) -> dict:
        """Status.sources details of the source (never a secret)."""
        ...

    async def keepalive(self) -> None:
        """Keep credentials from lapsing while nothing is fetched (called hourly; failures show in status)."""
        ...

    async def fetch_chain(self, symbol: str, final: bool = False) -> ChainSnapshot:
        """Current chain of `symbol` (an empty `contracts` tuple: no listed options; `final`: the closing
        snapshot). Raises NotListed, or net.FetchError on failure."""
        ...


def _questrade() -> ChainSource:
    from .questrade import instance
    return instance()


REGISTRY: dict[str, Callable[[], ChainSource]] = {"questrade": _questrade}


def tag(name: str) -> str:
    """The `source` tag rows from chain source `name` are stored with."""
    return BRIDGE_TAG if name == BRIDGE else name


def server_source_names() -> list[str]:
    """Names of the configured server-side ChainSources. Unknown name -> ValueError."""
    names = [n for n in get_settings().chain_sources if n != BRIDGE]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"OPTIONLENS_CHAIN_SOURCES: {unknown[0]!r} is not a known chain source "
                         f"(known: {', '.join([BRIDGE, *sorted(REGISTRY)])})")
    return names


def server_sources() -> list[ChainSource]:
    """The configured server-side ChainSources (available or not)."""
    return [REGISTRY[n]() for n in server_source_names()]


def available() -> list[ChainSource]:
    """The server-side ChainSources that can fetch now."""
    return [s for s in server_sources() if s.unavailable() is None]
