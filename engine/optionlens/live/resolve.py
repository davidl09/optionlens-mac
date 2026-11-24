"""A subscription request -> Wanted (reads the served chains: blocking, call from a worker thread)."""
from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

from ..models import normalize_symbol
from ..repo import options as options_repo
from ..sources.osi import INDEX_ROOTS, parse_osi
from .subscriptions import Wanted

MAX_SYMBOLS = 500
MAX_CONTRACTS = 1_000

_ROOT_OF = {root: sym for sym, roots in INDEX_ROOTS.items() for root in roots}


def _underlying(osi: str, served: dict[str, str]) -> str | None:
    if osi in served:
        return served[osi]
    try:
        root = parse_osi(osi)[0]
    except ValueError:
        return None
    return _ROOT_OF.get(root, root)


def resolve(symbols: Iterable[str], osis: Sequence[str],
            views: Iterable[tuple[str, date, float | None, float | None]]) -> Wanted:
    """Underlyings (canonical symbols), contracts by OSI (underlying from the served chain, else the OSI root) and
    chain views (symbol, expiry, strike_min, strike_max: the served contracts of that expiry in the band; the view's
    symbol is wanted too). Capped at MAX_SYMBOLS / MAX_CONTRACTS."""
    syms = {normalize_symbol(s) for s in symbols if s.strip()}
    contracts: dict[str, str] = {}
    osis = [o.strip().upper() for o in osis if o.strip()]
    served = options_repo.symbols_of(osis)
    for osi in osis:
        if (u := _underlying(osi, served)) is not None:
            contracts[osi] = u
    for sym, expiry, lo, hi in views:
        s = normalize_symbol(sym)
        syms.add(s)
        for osi in options_repo.osis_in_band(s, expiry, lo, hi):
            contracts[osi] = s
    kept = dict(list(contracts.items())[:MAX_CONTRACTS])
    return Wanted(frozenset(sorted(syms)[:MAX_SYMBOLS]), kept)
