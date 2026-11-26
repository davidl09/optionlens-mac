"""QuestradeSource: the ChainSource "questrade" (sources/chain_source.py).

One chain = (cached) symbol id + (daily) symbol info + 3 market-data calls:
  GET v1/symbols/search?prefix=SYM  once per symbol, cached in DATA_DIR/questrade_symbols.json (not-found answers
                                    are asked again after NEGATIVE_TTL; entries of an older ID_VERSION once more):
                                    the best of its listings (mapping.listings: the type that fits the symbol first)
  GET v1/symbols/:id                once per New York day: hasOptions, prevDayClosePrice and the reference data
                                    (fundamentals, current dividend: cached_reference, stored by services.reference)
  GET v1/markets/quotes?ids=:id     the underlying quote (strike band centre)
  GET v1/symbols/:id/options        the chain structure (expiries, strikes, option symbol ids), of the same id as the
                                    quote (an index's options too: SPX.IN's chain is asked for by SPX.IN's id)
When the cached listing lists no options (hasOptions false, or no standard contracts), the symbol's other listings in
symbols/search are tried once per New York day (symbols/:id, and symbols/:id/options where hasOptions): the first with
options is adopted and cached. One log line per symbol says what was tried and chosen.
  POST v1/markets/quotes/options    quotes + greeks of the first `expiries` expiries within ±`moneyness` (one
                                    filter per expiry, all in one request)
Extended-hours quotes of many symbols (fetch_extended):
  GET v1/markets/quotes?ids=…       MAX_IDS ids per call, only symbols whose id is cached (no searches)
Daily bars of Canadian listings (fetch_daily; Nasdaq does not cover them):
  GET v1/markets/candles/:id        interval OneDay, the same MAX_CANDLES limit and continuation. Not documented:
                                    how far back daily candles go and whether they are split / dividend adjusted
                                    (stored as returned; the weekly refresh replaces the history when it changes)
Intraday bars (fetch_candles):
  GET v1/markets/candles/:id        OHLCV candles of startTime..endTime at `interval`; at most MAX_CANDLES per
                                    response ("This call is limited to returning 2,000 candlesticks in a single
                                    response"), so longer spans are asked for window by window (a Market Data call:
                                    the same rate gate as every other call)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from ...analytics import intraday
from ...analytics.markets import calendar_for
from ...analytics.calendar import NY, now_ny
from ...fileio import read_json, write_json_atomic
from ...models import Bar, ChainSnapshot, Dividend, ExtendedQuote, Fundamentals, IntradayBar
from ...net import FetchError
from ..chain_source import NotListed
from ..osi import format_osi
from .auth import TokenError, TokenManager, TokenStore
from .client import QuestradeClient
from .errors import QuestradeError
from .mapping import (CANDLE_INTERVAL, DAILY_INTERVAL, Listed, SymbolInfo, candle_time, candles, current_dividend,
                      daily_bars, extended_quote, fundamentals, listed_contracts, option_filters, option_quotes,
                      listings, parse_quote, pick_expiries, snapshot, symbol_info)
from .ratelimit import RateGate

log = logging.getLogger(__name__)

NAME = "questrade"
NEGATIVE_TTL = timedelta(days=7)
ID_VERSION = 2   # how listings are chosen (mapping.listings); cached ids of an older version are resolved again
VERIFY_SYMBOL = "SPY"   # a liquid symbol every market-data permission can search
MAX_CANDLES = 2000   # documented per-response limit of markets/candles
MAX_IDS = 100        # ids per markets/quotes call (the docs give no maximum; 100 keeps URLs small)
KEEPALIVE_S = 24 * 3600   # refresh an idle token daily: an unused refresh token lapses (reportedly after ~7 days)


def _log_fetch(symbol: str, interval: str, end: datetime, got: list[IntradayBar]) -> None:
    """One candles answer: the window end asked for, the count, the newest candle and how far it is behind now
    (Questrade's candles end minutes before now; the newest minutes come from live quotes, services/intraday.py)."""
    now = datetime.now(timezone.utc)
    newest = max((c.ts for c in got), default=None)
    log.info("questrade candles %s %s: asked up to %s, got %d, newest %s (%s)", symbol, interval,
             end.astimezone(timezone.utc).isoformat(timespec="seconds"), len(got),
             newest.astimezone(timezone.utc).isoformat(timespec="seconds") if newest else "none",
             f"{(now - newest).total_seconds():.0f} s before now" if newest else "-")


class SymbolIds:
    """OptionLens symbol -> Questrade symbolId (None: not listed, checked at), persisted as JSON. Entries resolved by
    an older ID_VERSION (before listings preferred the type that fits the symbol) are dropped on load, so every symbol
    is resolved once more."""

    def __init__(self, path: Path):
        self.path = path
        raw = read_json(path)
        self._ids: dict[str, dict] = {s: e for s, e in (raw if isinstance(raw, dict) else {}).items()
                                      if isinstance(e, dict) and e.get("v") == ID_VERSION}

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
        self._ids[symbol] = {"id": sid, "checked_at": now.isoformat(timespec="seconds"), "v": ID_VERSION}
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
        self._probed: dict[str, date] = {}   # symbol -> the New York day its other listings were last tried
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
            found = listings(await self.api.get("symbols/search", {"prefix": symbol}), symbol)
            sid = found[0].symbol_id if found else None
            self.ids.put(symbol, sid, now)
            log.info("questrade %s: resolved to %s%s", symbol, found[0].describe() if found else "nothing (not listed)",
                     f"; other listings: {', '.join(x.describe() for x in found[1:])}" if len(found) > 1 else "")
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
        """hasOptions, prevDayClosePrice and the reference data of `symbol` (symbols/:id once per New York day).
        Raises NotListed, QuestradeError."""
        return await self._symbol_info(symbol, await self.symbol_id(symbol), now_ny().date())

    def cached_reference(self, day: date) -> dict[str, tuple[Fundamentals, Dividend | None]]:
        """{symbol: (reference snapshot, current dividend or None)} of the symbol infos fetched on New York day `day`
        (by the sweeps and live quotes): no call."""
        return {s: (fundamentals(info, s, day), current_dividend(info, s)) for s, (d, info) in self._info.items()
                if d == day}

    async def fetch_extended(self, symbols: list[str]) -> list[ExtendedQuote]:
        """Level 1 quotes of the `symbols` whose Questrade id is cached (the sweeps searched them; no searches here),
        MAX_IDS per call. Raises QuestradeError."""
        now = datetime.now(timezone.utc)
        ids = {sid: s for s in symbols if type(sid := self.ids.get(s, now)) is int}   # not None / False (bool)
        out: list[ExtendedQuote] = []
        order = sorted(ids)
        for i in range(0, len(order), MAX_IDS):
            chunk = order[i:i + MAX_IDS]
            payload = await self.api.get("markets/quotes", {"ids": ",".join(map(str, chunk))})
            at = datetime.now(timezone.utc)
            for q in payload.get("quotes") or []:
                sym = ids.get(int(q.get("symbolId", -1)))
                if sym is not None:
                    out.append(extended_quote(q, sym, at))
        return out

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

    async def _listed(self, symbol: str, sid: int, info: SymbolInfo) -> tuple[dict[date, str], dict[int, Listed]]:
        """The standard contracts listing `sid` has ({}, {} without hasOptions: not asked)."""
        if not info.has_options:
            return {}, {}
        return listed_contracts(await self.api.get(f"symbols/{sid}/options"), symbol)

    async def _optioned_listing(self, symbol: str, sid: int, today: date) -> tuple[int, SymbolInfo, tuple]:
        """(symbolId, info, (expiries, listed)) of the listing of `symbol` the chain comes from: the cached one, or,
        when it lists no options, the first of the symbol's other listings that does (tried once per New York day;
        adopted and cached). One log line says what was tried. Raises QuestradeError."""
        info = await self._symbol_info(symbol, sid, today)
        chain = await self._listed(symbol, sid, info)
        if chain[1] or self._probed.get(symbol) == today:
            return sid, info, chain
        self._probed[symbol] = today
        tried = [f"symbolId {sid} ({info.security_type or '?'}, {info.name or 'no description'}, "
                 f"{info.currency or '?'} {info.listing_exchange or '?'}) hasOptions={info.has_options}, "
                 f"{len(chain[1])} standard contracts"]
        found = None
        for c in listings(await self.api.get("symbols/search", {"prefix": symbol}), symbol):
            if c.symbol_id == sid:
                continue
            try:
                c_info = symbol_info(await self.api.get(f"symbols/{c.symbol_id}"))
                c_chain = await self._listed(symbol, c.symbol_id, c_info)
            except QuestradeError as e:
                if e.kind != "other":
                    raise
                tried.append(f"{c.describe()} refused: {e.reason}")
                continue
            tried.append(f"{c.describe()} hasOptions={c_info.has_options}, {len(c_chain[1])} standard contracts")
            if c_chain[1]:
                found = (c.symbol_id, c_info, c_chain)
                break
        if found is None:
            log.info("questrade %s: lists no options; tried %s", symbol, "; ".join(tried))
            return sid, info, chain
        self.ids.put(symbol, found[0], datetime.now(timezone.utc))
        self._info[symbol] = (today, found[1])
        self._option_ids.pop(symbol, None)
        log.info("questrade %s: adopted symbolId %d (it lists options); tried %s", symbol, found[0], "; ".join(tried))
        return found

    async def fetch_chain(self, symbol: str, final: bool = False) -> ChainSnapshot:
        """The chain of `symbol` (no contracts: no listing of it at Questrade lists options). Raises NotListed,
        FetchError."""
        today = now_ny().date()
        sid, info, (expiries, listed) = await self._optioned_listing(symbol, await self.symbol_id(symbol), today)
        quotes = (await self.api.get("markets/quotes", {"ids": str(sid)})).get("quotes") or []
        fetched = datetime.now(timezone.utc)
        if not quotes:
            raise FetchError(f"questrade:markets/quotes/{sid}", None, f"no quote for {symbol}")
        quote = parse_quote(quotes[0], symbol, info.prev_close, fetched, final)
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

    async def fetch_candles(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[IntradayBar]:
        """Candles of `symbol` starting in [start, end] (every hour Questrade has), ascending. One request per
        intraday.request_windows window; a response of MAX_CANDLES candles may be cut short, so the rest of its window
        is asked for from after its last candle. Raises NotListed, QuestradeError."""
        sid = await self.symbol_id(symbol)
        step = timedelta(seconds=intraday.INTERVAL_S[interval])
        out: dict[datetime, IntradayBar] = {}
        for a, b in intraday.request_windows((start, end), interval, MAX_CANDLES, calendar_for(symbol)):
            while a < b:
                got = candles(await self.api.get(f"markets/candles/{sid}", {
                    "startTime": candle_time(a), "endTime": candle_time(b), "interval": CANDLE_INTERVAL[interval]}))
                _log_fetch(symbol, interval, b, got)
                out.update((c.ts, c) for c in got if start <= c.ts <= end)
                if len(got) < MAX_CANDLES:
                    break
                a = max(c.ts for c in got) + step
        return [out[t] for t in sorted(out)]

    async def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        """Daily bars of `symbol` in [start, end], ascending; a response of MAX_CANDLES candles is continued after its
        last day. Raises NotListed, QuestradeError."""
        sid = await self.symbol_id(symbol)
        a, b = datetime.combine(start, time(0), NY), datetime.combine(end, time(23, 59, 59), NY)
        out: dict[date, Bar] = {}
        while a <= b:
            payload = await self.api.get(f"markets/candles/{sid}", {
                "startTime": candle_time(a), "endTime": candle_time(b), "interval": DAILY_INTERVAL})
            got = daily_bars(payload, symbol)
            out.update((x.date, x) for x in got if start <= x.date <= end)
            if len(payload.get("candles") or []) < MAX_CANDLES or not got:
                break
            a = datetime.combine(got[-1].date + timedelta(days=1), time(0), NY)
        return [out[d] for d in sorted(out)]
