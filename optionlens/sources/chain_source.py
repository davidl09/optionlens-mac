"""Server-side option-chain sources (vendors). None is configured by default: chains then come only from
the IBKR bridge through the ingest API, and the scheduler runs no chain sweeps.

A vendor module implements ChainSource and adds a factory to REGISTRY under the name the
`OPTIONLENS_CHAIN_SOURCE` setting selects. Chains it returns are stored with source tag SOURCE_TAG.
"""
from __future__ import annotations

from typing import Callable, Protocol

from ..config import get_settings
from ..models import ChainSnapshot

SOURCE_TAG = "vendor"      # `source` of rows stored from any server-side vendor (docs/INTEGRATION.md §5)
NONE = "none"


class ChainSource(Protocol):
    name: str

    async def fetch_chain(self, symbol: str) -> ChainSnapshot:
        """Current chain of `symbol` (an empty `contracts` tuple: no listed options). Raises
        net.FetchError on failure."""
        ...


REGISTRY: dict[str, Callable[[], ChainSource]] = {}


def configured() -> ChainSource | None:
    """The ChainSource the settings select, or None ("none", the default). Unknown name -> ValueError."""
    name = getattr(get_settings(), "chain_source", NONE) or NONE
    if name == NONE:
        return None
    try:
        return REGISTRY[name]()
    except KeyError:
        raise ValueError(f"OPTIONLENS_CHAIN_SOURCE={name!r} is not a known chain source "
                         f"(known: {', '.join(sorted(REGISTRY)) or 'none'})") from None
