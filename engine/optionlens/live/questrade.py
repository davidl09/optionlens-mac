"""QuestradeLive: keeps the hub's wanted set live from Questrade (docs/INTEGRATION.md §9).

Two modes (settings.questrade_live):
- "poll" (default): Level 1 snapshots of exactly the wanted set, GET v1/markets/quotes?ids= (underlyings) and POST
  v1/markets/quotes/options {"optionIds"} (contracts), at most MAX_IDS ids per call, every `cadence()` seconds. All
  calls go through the source's RateGate (≤ 70% of the documented market-data limits, shared with the sweeps); the
  cadence keeps live polling within the hourly budget left after SWEEP_RESERVE_PER_HOUR for the sweeps.
- "stream": Level 1 WebSocket streams (live/stream.py), one for the underlyings and one for the contracts, renewed
  before the access token expires and reopened after a drop with backoff. After STREAM_FAILURES failed attempts in a
  row the driver polls for STREAM_COOLDOWN_S, then tries streaming again. Questrade documents that L1 streaming through
  the API freezes the market data of any other IQ platform the user has open at the same time: hence opt-in.

Ids: underlying symbolIds from the source's cache (symbols/search once per symbol), option symbolIds from the chain
structure (symbols/:id/options once per underlying per New York day). Quotes map like the sweeps' (mapping.live_quote /
live_option); stream messages are merged per symbolId first (they may carry only changed fields).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, Literal, Mapping, TypeVar

from ..sources.chain_source import NotListed
from ..sources.questrade.auth import TokenError
from ..sources.questrade.errors import QuestradeError
from ..sources.questrade.mapping import Listed, live_option, live_quote, option_quotes
from .stream import Kind, QuoteStream, StreamError
from .subscriptions import EMPTY, Wanted

log = logging.getLogger("optionlens.live.questrade")

Mode = Literal["poll", "stream"]
MAX_IDS = 100                       # ids per quote call (the docs give no maximum; 100 keeps URLs and bodies small)
MIN_INTERVAL_S = 2.0                # never poll the same set faster
SWEEP_RESERVE_PER_HOUR = 6_000      # of the gate's 10,500/h: the day's first sweep needs ~5,800 calls
MIN_CHANGE_GAP_S = 1.0              # a changed set is polled at once, but not more often than this
STREAM_FAILURES = 3
STREAM_COOLDOWN_S = 15 * 60.0
STREAM_STABLE_S = 60.0              # a stream that stayed open this long resets the failure count
STREAM_DEBOUNCE_S = 1.0             # wait for the set to settle before reopening streams
BACKOFF_MAX_S = 30.0

T = TypeVar("T")
Publish = Callable[[str, Mapping[str, dict], Mapping[str, dict]], None]


def chunks(items: list[T], n: int = MAX_IDS) -> list[list[T]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def calls_per_round(n_symbols: int, n_contracts: int) -> int:
    return math.ceil(n_symbols / MAX_IDS) + math.ceil(n_contracts / MAX_IDS)


def cadence(calls: int, hour_budget: int) -> float:
    """Seconds between poll rounds of `calls` calls: the live share of the gate's hourly budget (what is left after
    the sweeps' reserve) spread evenly, never under MIN_INTERVAL_S."""
    per_s = max(1, hour_budget - SWEEP_RESERVE_PER_HOUR) / 3600.0
    return max(MIN_INTERVAL_S, calls / per_s)


class QuestradeLive:
    name = "questrade"

    def __init__(self, source, publish: Publish, mode: Mode = "poll", clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep, stream: QuoteStream | None = None):
        self.source, self.publish, self.mode, self.clock, self.sleep = source, publish, mode, clock, sleep
        self._stream = stream
        self._wanted: Wanted = EMPTY
        self._changed = asyncio.Event()
        self.ok_at: float | None = None
        self.error: str | None = None
        self.interval_s: float | None = None
        self.rounds = 0
        self._poll_until = 0.0
        self._stream_failures = 0
        self._open: set[Kind] = set()
        self._expect: set[Kind] = set()
        self._raw: dict[int, dict] = {}

    # ------------------------------------------------------------------ Driver contract (live/hub.py)
    def usable(self) -> str | None:
        return self.source.unavailable()

    def set_wanted(self, w: Wanted) -> None:
        if w != self._wanted:
            self._wanted = w
            self._changed.set()

    def streaming(self) -> bool:
        return self.mode == "stream" and self.clock() >= self._poll_until

    def health(self) -> dict:
        if self.streaming() and self._expect and self._open >= self._expect:
            self.ok_at = self.clock()       # an open, authenticated stream is live even while nothing trades
        return {"ok_at": self.ok_at, "error": self.error, "mode": "stream" if self.streaming() else "poll",
                "interval_s": None if self.streaming() else self.interval_s}

    async def run(self) -> None:
        while True:
            if self.streaming():
                await self._stream_until_failed()
            else:
                await self._poll_round()

    # ------------------------------------------------------------------ ids
    async def _symbol_ids(self, symbols: Iterable[str]) -> dict[str, int]:
        out = {}
        for s in sorted(symbols):
            try:
                out[s] = await self.source.symbol_id(s)
            except NotListed:
                continue
            except QuestradeError as e:
                if e.kind == "auth":
                    raise
                log.info("live: no Questrade id for %s (%s)", s, e.reason)
        return out

    async def _option_ids(self, contracts: Mapping[str, str]) -> dict[int, tuple[str, str, Listed]]:
        """{option symbolId: (osi, underlying, Listed)} of the wanted contracts Questrade lists."""
        by_under: dict[str, list[str]] = {}
        for osi, under in contracts.items():
            by_under.setdefault(under, []).append(osi)
        out = {}
        for under, osis in sorted(by_under.items()):
            try:
                ids = await self.source.option_ids(under)
            except NotListed:
                continue
            except QuestradeError as e:
                if e.kind == "auth":
                    raise
                log.info("live: no Questrade option ids for %s (%s)", under, e.reason)
                continue
            for osi in osis:
                if osi in ids:
                    qid, listed = ids[osi]
                    out[qid] = (osi, under, listed)
        return out

    async def _prev_closes(self, symbols: Iterable[str]) -> dict[str, float | None]:
        out = {}
        for s in symbols:
            try:
                out[s] = (await self.source.symbol_info(s)).prev_close
            except (NotListed, QuestradeError):
                out[s] = None
        return out

    # ------------------------------------------------------------------ quotes -> patches
    def _equity(self, quotes: list[dict], sym_of: Mapping[int, str], prev: Mapping[str, float | None]) -> None:
        at = datetime.now(timezone.utc)
        patches = {}
        for q in quotes:
            sid = int(q["symbolId"])
            if (sym := sym_of.get(sid)) is None:
                continue
            raw = self._raw.setdefault(sid, {})
            raw.update(q)
            patches[sym] = live_quote(raw, sym, prev.get(sym), at)
        if patches:
            self.publish(self.name, patches, {})

    def _options(self, quotes: list[dict], opt: Mapping[int, tuple[str, str, Listed]]) -> None:
        patches = {}
        for q in quotes:
            qid = int(q["symbolId"])
            if (hit := opt.get(qid)) is None:
                continue
            raw = self._raw.setdefault(qid, {})
            raw.update(q)
            osi, under, listed = hit
            patches[osi] = live_option(under, listed, raw)
        if patches:
            self.publish(self.name, {}, patches)

    def _ok(self) -> None:
        self.ok_at, self.error = self.clock(), None

    def _fail(self, why: str) -> None:
        self.error = why
        log.warning("live: Questrade %s", why)

    # ------------------------------------------------------------------ polling
    async def poll_once(self, w: Wanted) -> int:
        """One snapshot round of `w`. Returns the quote calls made."""
        calls = 0
        sids = await self._symbol_ids(w.symbols)
        prev = await self._prev_closes(sids)
        sym_of = {sid: s for s, sid in sids.items()}
        for chunk in chunks(sorted(sym_of)):
            payload = await self.source.api.get("markets/quotes", {"ids": ",".join(map(str, chunk))})
            calls += 1
            self._equity(list(payload.get("quotes") or []), sym_of, prev)
        opt = await self._option_ids(w.contracts)
        for chunk in chunks(sorted(opt)):
            payload = await self.source.api.post("markets/quotes/options", {"optionIds": chunk})
            calls += 1
            self._options(list(option_quotes(payload).values()), opt)
        return calls

    async def _wait_change(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def _poll_round(self) -> None:
        w = self._wanted
        if w.empty:
            await self._wait_change(5.0)
            self._changed.clear()
            return
        self._changed.clear()
        started = self.clock()
        calls = calls_per_round(len(w.symbols), len(w.contracts))
        try:
            calls = await self.poll_once(w) or calls
            self._ok()
            self.rounds += 1
        except QuestradeError as e:
            self._fail(e.reason)
        except TokenError as e:
            self._fail(str(e))
        self.interval_s = cadence(calls, self.source.gate.hour_budget)
        if await self._wait_change(max(0.0, started + self.interval_s - self.clock())):
            await self.sleep(max(0.0, started + MIN_CHANGE_GAP_S - self.clock()))

    # ------------------------------------------------------------------ streaming
    def _streamer(self) -> QuoteStream:
        if self._stream is None:
            self._stream = QuoteStream(self.source.api, self.source.tokens)
        return self._stream

    async def _stream_forever(self, kind: Kind, ids: list[int], on_quotes: Callable[[list[dict]], None]) -> None:
        """Keep one stream open: renew it when it returns (token about to expire), reopen it after a drop with
        backoff; give up (raise) after STREAM_FAILURES failed attempts in a row."""
        failures = 0
        while True:
            opened_at: list[float] = []

            def opened() -> None:
                opened_at.append(self.clock())
                self._open.add(kind)
                self._ok()
            try:
                await self._streamer().run(kind, ids, on_quotes, opened)
                failures = 0
            except (StreamError, QuestradeError, TokenError) as e:
                if isinstance(e, QuestradeError) and e.kind == "auth":
                    raise
                stable = bool(opened_at) and self.clock() - opened_at[0] >= STREAM_STABLE_S
                failures = 1 if stable else failures + 1
                self._fail(str(e) if not isinstance(e, QuestradeError) else e.reason)
                if failures >= STREAM_FAILURES:
                    raise StreamError(f"{kind} stream failed {failures} times in a row") from e
                await self.sleep(min(BACKOFF_MAX_S, 2.0 ** failures))
            finally:
                self._open.discard(kind)

    async def _stream_until_failed(self) -> None:
        """Streams for the current set until it changes (then reopened) or a stream gives up (then poll for
        STREAM_COOLDOWN_S)."""
        w = self._wanted
        self._changed.clear()
        if w.empty:
            await self._wait_change(5.0)
            return
        try:
            sids = await self._symbol_ids(w.symbols)
            prev = await self._prev_closes(sids)
            opt = await self._option_ids(w.contracts)
        except (QuestradeError, TokenError) as e:
            self._fail(getattr(e, "reason", str(e)))
            await self.sleep(5.0)
            return
        sym_of = {sid: s for s, sid in sids.items()}
        tasks = []
        self._expect = set()
        if sym_of:
            self._expect.add("equity")
            tasks.append(asyncio.create_task(self._stream_forever(
                "equity", sorted(sym_of), lambda qs: self._equity(qs, sym_of, prev))))
        if opt:
            self._expect.add("option")
            tasks.append(asyncio.create_task(self._stream_forever(
                "option", sorted(opt), lambda qs: self._options(qs, opt))))
        if not tasks:
            await self._wait_change(30.0)
            return
        changed = asyncio.create_task(self._changed.wait())
        try:
            done, _ = await asyncio.wait([*tasks, changed], return_when=asyncio.FIRST_COMPLETED)
            if changed in done:
                await self.sleep(STREAM_DEBOUNCE_S)
                return
            for t in done:
                if (e := t.exception()) is not None:
                    if isinstance(e, QuestradeError):
                        raise e
                    self._poll_until = self.clock() + STREAM_COOLDOWN_S
                    log.warning("live: streaming failed (%s); polling for %.0f min", e, STREAM_COOLDOWN_S / 60)
        finally:
            for t in [*tasks, changed]:
                t.cancel()
            await asyncio.gather(*tasks, changed, return_exceptions=True)
            self._expect = set()
            self._open.clear()
