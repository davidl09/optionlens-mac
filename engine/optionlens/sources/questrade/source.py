"""QuestradeSource: the ChainSource "questrade" (sources/chain_source.py).

One chain = (cached) symbol id + (daily) symbol info + 3 market-data calls:
  GET v1/symbols/search?prefix=SYM  once per symbol, cached in DATA_DIR/questrade_symbols.json (not-found answers
                                    are asked again after NEGATIVE_TTL)
  GET v1/symbols/:id                once per New York day: hasOptions, prevDayClosePrice
  GET v1/markets/quotes?ids=:id     the underlying quote (strike band centre)
  GET v1/symbols/:id/options        the chain structure (expiries, strikes, option symbol ids)
  POST v1/markets/quotes/options    quotes + greeks of the first `expiries` expiries within ±`moneyness` (one
                                    filter per expiry, all in one request)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from ...analytics.calendar import now_ny
from ...fileio import read_json, write_json_atomic
from ...models import ChainSnapshot
from ...net import FetchError
from ..chain_source import NotListed
from ..osi import format_osi
from .auth import TokenError, TokenManager, TokenStore
from .client import QuestradeClient
from .errors import QuestradeError
from .mapping import (Listed, SymbolInfo, listed_contracts, option_filters, option_quotes, parse_quote, pick_expiries,
                      pick_symbol_id, snapshot, symbol_info)
from .ratelimit import RateGate

log = logging.getLogger(__name__)

NAME = "questrade"
NEGATIVE_TTL = timedelta(days=7)
VERIFY_SYMBOL = "SPY"   # a liquid symbol every market-data permission can search
KEEPALIVE_S = 24 * 3600   # refresh an idle token daily: an unused refresh token lapses (reportedly after ~7 days)


class SymbolIds:
    """OptionLens symbol -> Questrade symbolId (None: not listed, checked at), persisted as JSON."""

    def __init__(self, path: Path):
        self.path = path
        raw = read_json(path)
        self._ids: dict[str, dict] = raw if isinstance(raw, dict) else {}

    def get(self, symbol: str, now: datetime) -> int | None | bool:
        """The id, None (not listed, checked recently) or False (unknown / due for another look)."""
        e = self._ids.get(symbol)
        if e is None:
            return False
        if e.get("id") is not None:
            return int(e["id"])
        checked = datetime.fromisoformat(e["checked_at"])
        return None if now - checked < NEGATIVE_TTL else False

    def put(self, symbol: str, sid: int | None, now: datetime) -> None:
        self._ids[symbol] = {"id": sid, "checked_at": now.isoformat(timespec="seconds")}
        try:
            write_json_atomic(self.path, self._ids)
        except OSError as e:
            log.warning("questrade: could not save the symbol id cache: %s", e)


class QuestradeSource:
    name = NAME

    def __init__(self, data_dir: Path, expiries: int, moneyness: float, http: Callable[[], httpx.AsyncClient],
                 gate: RateGate | None = None):
        self.expiries, self.moneyness = expiries, moneyness
        self.tokens = TokenManager(TokenStore(data_dir / "secrets" / "questrade.json"), http)
        self.gate = gate or RateGate()
        self.api = QuestradeClient(self.tokens, self.gate, http)
        self.ids = SymbolIds(data_dir / "questrade_symbols.json")
        self._info: dict[str, tuple[date, SymbolInfo]] = {}
        self._option_ids: dict[str, tuple[date, dict[str, tuple[int, Listed]]]] = {}
        self.last_delayed: bool | None = None
        self.verification: dict | None = None   # result of the check after a token was saved (verify)

    def connected(self) -> bool:
        return self.tokens.state != "disconnected"

    def unavailable(self) -> str | None:
        """Why the source cannot fetch now (e.g. "not connected", needs attention), or None when it can."""
        return self.tokens.unusable()

    def problem(self) -> str | None:
        """The short reason the source needs attention (e.g. "403 code 1016 (missing market-data permission)")."""
        return self.tokens.reason if self.tokens.state == "needs_attention" else None

    def status(self) -> dict:
        """Connection state, call pacing, whether the last chain was delayed and the last token check (never a
        token)."""
        return {**self.tokens.status(), **self.gate.stats(), "delayed": self.last_delayed,
                "expiries": self.expiries, "moneyness": self.moneyness, "verification": self.verification}

    async def verify(self) -> dict:
        """One market-data call (symbols/search for VERIFY_SYMBOL) right after a token was saved, so a missing
        permission shows at once instead of in the next sweep: {ok, message, at}. An auth-class answer leaves the
        source needing attention (client.py)."""
        try:
            await self.api.get("symbols/search", {"prefix": VERIFY_SYMBOL})
            result = {"ok": True, "message": "Token OK: market data permitted"}
        except QuestradeError as e:
            result = {"ok": False, "message": e.attention or f"Token check failed: {e.reason}"}
        self.verification = {**result, "at": datetime.now(timezone.utc)}
        return self.verification

    async def keepalive(self) -> None:
        try:
            await self.tokens.keepalive(KEEPALIVE_S)
        except TokenError:
            pass   # recorded in the token state (status)

    async def symbol_id(self, symbol: str) -> int:
        now = datetime.now(timezone.utc)
        sid = self.ids.get(symbol, now)
        if sid is False:
            sid = pick_symbol_id(await self.api.get("symbols/search", {"prefix": symbol}), symbol)
            self.ids.put(symbol, sid, now)
        if sid is None:
            raise NotListed(symbol)
        return int(sid)

    async def _symbol_info(self, symbol: str, sid: int, today: date) -> SymbolInfo:
        cached = self._info.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        info = symbol_info(await self.api.get(f"symbols/{sid}"))
        self._info[symbol] = (today, info)
        return info

    async def symbol_info(self, symbol: str) -> SymbolInfo:
        """hasOptions and prevDayClosePrice of `symbol` (symbols/:id once per New York day). Raises NotListed,
        QuestradeError."""
        return await self._symbol_info(symbol, await self.symbol_id(symbol), now_ny().date())

    async def option_ids(self, symbol: str) -> dict[str, tuple[int, Listed]]:
        """{OSI: (option symbolId, Listed)} of the standard contracts Questrade lists for `symbol` (symbols/:id/options
        once per New York day; live quotes ask for option quotes by these ids). Raises NotListed, QuestradeError."""
        today = now_ny().date()
        cached = self._option_ids.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        _, listed = listed_contracts(await self.api.get(f"symbols/{await self.symbol_id(symbol)}/options"), symbol)
        ids = {format_osi(symbol, c.expiry, c.right, c.strike): (qid, c) for qid, c in listed.items()}
        self._option_ids[symbol] = (today, ids)
        return ids

    async def fetch_chain(self, symbol: str, final: bool = False) -> ChainSnapshot:
        """The chain of `symbol` (no contracts: Questrade lists no options for it). Raises NotListed, FetchError."""
        sid = await self.symbol_id(symbol)
        today = now_ny().date()
        info = await self._symbol_info(symbol, sid, today)
        quotes = (await self.api.get("markets/quotes", {"ids": str(sid)})).get("quotes") or []
        fetched = datetime.now(timezone.utc)
        if not quotes:
            raise FetchError(f"questrade:markets/quotes/{sid}", None, f"no quote for {symbol}")
        quote = parse_quote(quotes[0], symbol, info.prev_close, fetched, final)
        if not info.has_options:
            return ChainSnapshot(quote, (), fetched, quote.delayed)
        expiries, listed = listed_contracts(await self.api.get(f"symbols/{sid}/options"), symbol)
        if not listed:
            return ChainSnapshot(quote, (), fetched, quote.delayed)
        spot = quote.price or info.prev_close
        if not spot:
            raise FetchError(f"questrade:markets/quotes/{sid}", None, f"no price for {symbol}")
        lo, hi = spot * (1 - self.moneyness), spot * (1 + self.moneyness)
        picks = set(pick_expiries(expiries, today, self.expiries))
        band = [c for c in listed.values() if c.expiry in picks and lo <= c.strike <= hi]
        filters = option_filters(sid, [expiries[d] for d in sorted(picks)], lo, hi)
        oq = option_quotes(await self.api.post("markets/quotes/options", {"filters": filters})) if filters else {}
        snap = snapshot(quote, band, oq, fetched)
        self.last_delayed = snap.delayed
        return snap
