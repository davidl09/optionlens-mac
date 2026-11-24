"""Canadian universe (docs/SPEC.md §5 `universe_ca`): the largest optionable TSX / TSX Venture stocks plus the
optionable Canadian ETFs, selected with Questrade.

Selection rule: every candidate of `canada.txt` (the equity and ETF option classes the Montréal Exchange lists) is
resolved with Questrade — the Toronto listing `<ticker>.TO`, else the TSX Venture one `<ticker>.VN`, CAD-traded
(mapping.pick_symbol_id) — and described with `symbols/:id` (name, market cap, hasOptions, indicated dividend
yield, sector). Kept: candidates Questrade lists options for (`hasOptions`); of the stocks the UNIVERSE_CA_STOCKS
(200) largest by Questrade's market cap, plus every ETF / trust. Weekly (Mondays) and when a Questrade token is
saved; without a usable Questrade connection the run is skipped, and when more than 10% of the lookups fail it
fails: the stored Canadian universe is kept either way.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from ..analytics import markets
from ..config import get_settings
from ..models import Bar
from ..repo import symbols as symbols_repo
from ..sources import chain_source
from ..sources.chain_source import NotListed
from ..sources.questrade.mapping import SymbolInfo
from ._util import log, run_bounded

CONCURRENCY = 4
MAX_FAILED_SHARE = 0.10   # more failed lookups than this: the run fails and the stored universe is kept
KINDS = ("stock", "etf")


@dataclass(frozen=True)
class Candidate:
    ticker: str     # TSX / TSXV ticker without suffix, e.g. TECK.B
    kind: str       # "stock" | "etf"
    name: str | None


@lru_cache(maxsize=4)
def candidates(path: Path | None = None) -> tuple[Candidate, ...]:
    """canada.txt -> candidates (comments and blank lines ignored; one ticker once)."""
    p = path or get_settings().ca_file
    out: dict[str, Candidate] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        body, _, comment = line.partition("#")
        parts = body.split()
        if not parts:
            continue
        kind = parts[1] if len(parts) > 1 else "stock"
        if kind not in KINDS:
            raise ValueError(f"{p.name}: bad kind {kind!r} for {parts[0]}")
        out.setdefault(parts[0].upper(), Candidate(parts[0].upper(), kind, comment.strip() or None))
    return tuple(out.values())


class SymbolDirectory(Protocol):
    """What Canadian listings need from Questrade (QuestradeSource implements it): descriptions and daily bars."""

    async def symbol_info(self, symbol: str) -> SymbolInfo:
        """symbols/:id of a canonical symbol; raises NotListed when Questrade has no such listing."""
        ...

    async def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        """Daily bars in [start, end] (jobs/prices.py); raises NotListed / QuestradeError."""
        ...


def directory() -> SymbolDirectory | None:
    """The usable Questrade source (connected, not needing attention), else None."""
    return next((s for s in chain_source.server_sources() if s.name == "questrade" and s.unavailable() is None), None)


def _sector(raw: str | None) -> str | None:
    """Questrade's industrySector ("BasicMaterials") as words ("Basic Materials")."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw) if raw else None


async def resolve(d: SymbolDirectory, ticker: str) -> tuple[str, SymbolInfo] | None:
    """(canonical symbol, Questrade info) of a Canadian ticker: the TSX listing, else the TSX Venture one; None when
    Questrade has neither (CAD-traded)."""
    for exchange in ("TSX", "TSXV"):
        sym = markets.canadian_symbol(ticker, exchange)
        try:
            return sym, await d.symbol_info(sym)
        except NotListed:
            continue
    return None


def symbol_row(symbol: str, info: SymbolInfo, kind: str, fallback_name: str | None = None) -> dict:
    """symbols-table row of a resolved Canadian listing."""
    return {"symbol": symbol, "name": info.name or fallback_name, "kind": kind, "sector": _sector(info.sector),
            "market_cap": info.market_cap, "dividend_yield": info.dividend_yield}


def select(resolved: list[tuple[Candidate, str, SymbolInfo]], n_stocks: int) -> list[str]:
    """The selection rule (module doc) over resolved candidates -> member symbols (stocks by market cap, then ETFs)."""
    optionable = [(c, s, i) for c, s, i in resolved if i.has_options]
    stocks = sorted((x for x in optionable if x[0].kind == "stock"),
                    key=lambda x: (x[2].market_cap is None, -(x[2].market_cap or 0.0), x[1]))[:n_stocks]
    etfs = sorted(s for c, s, _ in optionable if c.kind == "etf")
    return [s for _, s, _ in stocks] + etfs


async def universe_ca() -> str:
    """Refresh the Canadian universe (module doc)."""
    d = directory()
    if d is None:
        return "skipped: Questrade is not connected (the Canadian universe is selected with Questrade)"
    cands = candidates()
    resolved: list[tuple[Candidate, str, SymbolInfo]] = []
    missing: list[str] = []

    async def one(c: Candidate) -> None:
        got = await resolve(d, c.ticker)
        if got is None:
            missing.append(c.ticker)
        else:
            resolved.append((c, *got))

    _, failed = await run_bounded(cands, one, CONCURRENCY, "universe_ca")
    if not resolved or len(failed) > MAX_FAILED_SHARE * len(cands):   # keep the stored universe over a partial one
        raise RuntimeError(f"Questrade resolved {len(resolved)} of {len(cands)} Canadian candidates "
                           f"({len(failed)} failed); the stored Canadian universe is kept")
    members = select(resolved, get_settings().universe_ca_stocks)
    rows = [symbol_row(s, i, c.kind, c.name) for c, s, i in resolved]

    def store() -> int:
        symbols_repo.upsert_symbols(rows)
        n = symbols_repo.set_universe(members, markets.CA)
        symbols_repo.reprobe_not_optionable(members)
        return n

    n = await asyncio.to_thread(store)
    n_etf = sum(1 for c, s, _ in resolved if s in set(members) and c.kind == "etf")
    if missing:
        log.info("universe_ca: not on Questrade as a CAD TSX/TSXV listing: %s", ", ".join(sorted(missing)))
    return (f"Canadian universe {n} ({n - n_etf} stocks, {n_etf} ETFs) of {len(cands)} candidates; "
            f"{len(resolved) - sum(1 for _, _, i in resolved if i.has_options)} without options, {len(missing)} not "
            f"listed, {len(failed)} failed")


async def ensure_canadian(symbol: str) -> dict | None:
    """A symbols row for a Canadian symbol not stored yet, from Questrade (None when Questrade is unusable or does
    not list it)."""
    d = directory()
    if d is None:
        return None
    try:
        info = await d.symbol_info(symbol)
    except NotListed:
        return None
    kind = "etf" if symbol_kind_hint(symbol) == "etf" else "stock"
    await asyncio.to_thread(symbols_repo.upsert_symbols, [symbol_row(symbol, info, kind)])
    return await asyncio.to_thread(symbols_repo.get_symbol, symbol)


def symbol_kind_hint(symbol: str) -> str:
    """The candidate list's kind for a Canadian symbol, else "stock" (Questrade types ETFs as "Stock")."""
    t = markets.ticker(symbol)
    return next((c.kind for c in candidates() if c.ticker == t), "stock")
