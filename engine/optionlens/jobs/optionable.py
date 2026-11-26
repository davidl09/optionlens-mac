"""Whether a symbol lists options (symbols.optionable, docs/INTEGRATION.md §5 "Listed options"), from the chain
sources' answers. One source's "none" is that source's answer, never the symbol's:

- true once a chain of the symbol from any source is stored (chain_stored);
- false once every chain source serving the symbol answered "none" for it — 'no_options' (it lists none) or
  'not_listed' (it does not carry it), at least one 'no_options' (confirm_none);
- otherwise unknown (NULL): e.g. Questrade lists none but the IBKR bridge has not answered yet.

The sources serving a symbol: the IBKR bridge for US listings (chain_source.bridge_serves) when it is enabled and has
reported at least once (a bridge_status row: it is set up), and every enabled server-side source the user connected;
the source answering always counts. Each source's answer is kept (chain_verdicts) until that source delivers a chain or
the universe refresh asks again (symbols_repo.reprobe_not_optionable), and keeps that source from being asked for the
symbol in the background (answered_none: the sweeps, the bridge's chain universe); opening the symbol asks it again.
"""
from __future__ import annotations

from typing import Iterable, Literal

from ..config import get_settings
from ..repo import bridge as bridge_repo
from ..repo import symbols as symbols_repo
from ..sources import chain_source

Verdict = Literal["no_options", "not_listed"]


def serving(symbol: str) -> set[str]:
    """Source tags ('ibkr', 'questrade', ...) of the set-up chain sources that serve `symbol`."""
    out = {s.name for s in chain_source.server_sources() if s.connected()}
    if (chain_source.BRIDGE in get_settings().chain_sources and chain_source.bridge_serves(symbol)
            and (bridge_repo.get_status() or {}).get("client")):
        out.add(chain_source.BRIDGE_TAG)
    return out


def confirm_none(symbol: str, source: str, verdict: Verdict) -> bool | None:
    """`source` has no chain of `symbol` (`verdict`). Marks the symbol optionable=false when every serving source has
    now answered "none" (module doc). Returns the symbol's optionable flag."""
    symbols_repo.record_verdict(symbol, source, verdict)
    got = symbols_repo.verdicts(symbol)
    if "no_options" in got.values() and (serving(symbol) | {source}) <= set(got):
        symbols_repo.set_optionable(symbol, False)
    row = symbols_repo.get_symbol(symbol)
    return row["optionable"] if row else None


def chain_stored(symbol: str, source: str | None) -> None:
    """A chain of `symbol` from `source` was stored: optionable, and that source's "none" answer is void."""
    if source is not None:
        symbols_repo.clear_verdict(symbol, source)
    symbols_repo.set_optionable(symbol, True)


def answered_none(source: str, symbols: Iterable[str]) -> set[str]:
    """Of `symbols`, those `source` answered "none" for (not asked again in the background)."""
    return symbols_repo.answered_none(source) & set(symbols)
