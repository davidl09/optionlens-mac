"""QuestradeLive: keeps the hub's wanted set live from Questrade (docs/INTEGRATION.md §9).

Two modes (the user's setting, live/mode.py; switchable at runtime with set_mode):
- "poll" (default): Level 1 snapshots of exactly the wanted set, GET v1/markets/quotes?ids= (underlyings) and POST
  v1/markets/quotes/options {"optionIds"} (contracts), at most MAX_IDS ids per call, every `cadence()` seconds. All
  calls go through the source's RateGate (≤ 70% of the documented market-data limits, shared with the sweeps); the
  cadence keeps live polling within the hourly budget left after SWEEP_RESERVE_PER_HOUR for the sweeps.
- "stream": ONE Level 1 WebSocket stream (live/stream.py) for the union of underlyings and contracts: Questrade allows
  one streaming connection per session (a second closes the first) and it carries both kinds' messages (no per-stream
  limit is documented or observed, so the set is not split). The stream is renewed (new stream requests, fresh token)
  before the access token expires or when it was refreshed, reopened after a drop with backoff, and reopened for a
  changed set (the old connection fully closed first, then the stream requests of both kinds, then one connect).
  Streaming falls back to polling (`fallback` says why) when a handshake fails, when the stream sends nothing within
  STREAM_SILENT_S in the regular session, or after DROP_LIMIT drops within DROP_WINDOW_S; streaming is tried again
  after STREAM_RETRY_S. Questrade documents that L1 streaming through
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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, Mapping, Sequence, TypeVar

from ..sources.chain_source import NotListed
from ..sources.questrade.auth import TokenError
from ..sources.questrade.errors import QuestradeError
from ..sources.questrade.mapping import Listed, live_option, live_quote, option_quotes
from .mode import MODES, Mode
from .session import session_at
from .stream import HandshakeError, Kind, QuoteStream, StreamDropped, StreamSilent
from .subscriptions import EMPTY, Wanted

log = logging.getLogger("optionlens.live.questrade")

MAX_IDS = 100                       # ids per quote call (the docs give no maximum; 100 keeps URLs and bodies small)
MIN_INTERVAL_S = 2.0                # never poll the same set faster
SWEEP_RESERVE_PER_HOUR = 6_000      # of the gate's 10,500/h: the day's first sweep needs ~5,800 calls
MIN_CHANGE_GAP_S = 1.0              # a changed set is polled at once, but not more often than this
STREAM_SILENT_S = 15.0              # regular session: a stream silent this long after its handshake -> poll
DROP_LIMIT = 3                      # this many drops ...
DROP_WINDOW_S = 5 * 60.0            # ... within this window -> poll
STREAM_RETRY_S = 5 * 60.0           # after a fallback, try streaming again this much later
STREAM_DEBOUNCE_S = 1.0             # wait for the set to settle before reopening the stream
BACKOFF_MAX_S = 30.0
RATE_WINDOW_S = 10.0                # messages/s over this window

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


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _regular_now() -> bool:
    return session_at(datetime.now(timezone.utc)) == "regular"


@dataclass
class Conn:
    """The stream connection (diagnostics): `ids` per kind it carries."""
    ids: dict[Kind, int]
    open: bool = False
    port: int | None = None
    opened_at: float | None = None
    last_message_at: float | None = None
    messages: int = 0
    connects: int = 0


class _FallBack(Exception):
    """Streaming gives way to polling: `reason` is short (shown in the header), `detail` the error."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


class QuestradeLive:
    name = "questrade"

    def __init__(self, source, publish: Publish, mode: Mode = "poll", clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep, stream: QuoteStream | None = None,
                 regular: Callable[[], bool] = _regular_now):
        if mode not in MODES:
            raise ValueError(f"live mode must be one of {MODES}, not {mode!r}")
        self.source, self.publish, self.mode, self.clock, self.sleep = source, publish, mode, clock, sleep
        self._stream, self._regular = stream, regular
        self._wanted: Wanted = EMPTY
        self._changed = asyncio.Event()
        self.ok_at: float | None = None
        self.error: str | None = None
        self.interval_s: float | None = None
        self.rounds = 0
        self._fallback: str | None = None       # why streaming gave way to polling (until _retry_at)
        self._retry_at = 0.0
        self._drops: deque[float] = deque()
        self._conn: Conn | None = None
        self._msg_at: deque[float] = deque()
        self._raw: dict[int, dict] = {}

    # ------------------------------------------------------------------ Driver contract (live/hub.py)
    def usable(self) -> str | None:
        return self.source.unavailable()

    def set_wanted(self, w: Wanted) -> None:
        if w != self._wanted:
            self._wanted = w
            self._changed.set()

    def set_mode(self, mode: Mode) -> None:
        """Switch between streaming and polling now (the running loop closes / opens the stream)."""
        if mode not in MODES:
            raise ValueError(f"live mode must be one of {MODES}, not {mode!r}")
        if mode != self.mode:
            log.info("live: Questrade live updates: %s -> %s", self.mode, mode)
            self.mode = mode
            self._fallback, self._retry_at = None, 0.0
            self._drops.clear()
            self._changed.set()

    def streaming(self) -> bool:
        return self.mode == "stream" and self.clock() >= self._retry_at

    def fallback(self) -> str | None:
        """Why the driver polls although streaming is chosen, or None."""
        return self._fallback if self.mode == "stream" and not self.streaming() else None

    def health(self) -> dict:
        streaming = self.streaming()
        if streaming and self._conn and self._conn.open:
            self.ok_at = self.clock()       # an open, authenticated stream is live even while nothing trades
        return {"ok_at": self.ok_at, "error": self.error, "mode": "stream" if streaming else "poll",
                "interval_s": None if streaming else self.interval_s, "fallback": self.fallback()}

    def diagnostics(self) -> dict:
        """The stream for Settings and live verification: `streams` holds the one connection (none when not
        streaming) with its ids per kind, open, port, messages and the last message time; messages/s over
        RATE_WINDOW_S; drops within DROP_WINDOW_S; the fallback and its retry."""
        now = self.clock()
        recent = sum(1 for t in self._msg_at if t >= now - RATE_WINDOW_S)
        conns = [self._conn] if self._conn else []
        last = max((c.last_message_at for c in conns if c.last_message_at), default=None)
        fb = self.fallback()
        return {"setting": self.mode, "streaming": self.streaming(), "fallback": fb,
                "fallback_detail": self.error if fb else None, "retry_at": _iso(self._retry_at) if fb else None,
                "messages_per_s": round(recent / RATE_WINDOW_S, 1), "last_message_at": _iso(last),
                "drops_recent": sum(1 for t in self._drops if t >= now - DROP_WINDOW_S),
                "streams": [{"ids": dict(c.ids), "open": c.open, "port": c.port,
                             "opened_at": _iso(c.opened_at), "last_message_at": _iso(c.last_message_at),
                             "messages": c.messages, "connects": c.connects} for c in conns]}

    async def run(self) -> None:
        while True:
            if self.streaming():
                await self._stream_session()
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

    def _fall_back(self, f: _FallBack) -> None:
        self._fallback, self._retry_at = f.reason, self.clock() + STREAM_RETRY_S
        self._drops.clear()
        self.error = f.detail
        log.warning("live: Questrade streaming fell back to polling (%s: %s); retrying in %.0f min", f.reason,
                    f.detail, STREAM_RETRY_S / 60)

    def _dropped(self, now: float) -> int:
        """Record a drop; the drops within DROP_WINDOW_S."""
        self._drops.append(now)
        while self._drops and self._drops[0] < now - DROP_WINDOW_S:
            self._drops.popleft()
        return len(self._drops)

    def _message(self, conn: Conn) -> None:
        now = self.clock()
        conn.messages += 1
        conn.last_message_at = now
        self._msg_at.append(now)
        while self._msg_at and self._msg_at[0] < now - RATE_WINDOW_S:
            self._msg_at.popleft()

    async def _stream_forever(self, conn: Conn, subs: Mapping[Kind, Sequence[int]],
                              on_quotes: Callable[[Kind, list[dict]], None]) -> None:
        """Keep the one stream of `subs` open: renewed (new stream requests, fresh token) when it returns, reopened
        after a drop with backoff. Raises _FallBack when streaming should give way to polling (a failed handshake, a
        silent stream in the regular session, DROP_LIMIT drops within DROP_WINDOW_S); QuestradeError for auth
        failures."""
        def opened(port: int) -> None:
            conn.open, conn.port, conn.opened_at = True, port, self.clock()
            conn.connects += 1
            self._ok()
        while True:
            silent_s = STREAM_SILENT_S if self._regular() else None
            try:
                await self._streamer().run(subs, on_quotes, opened, lambda: self._message(conn), silent_s)
            except QuestradeError as e:
                if e.kind == "auth":
                    raise
                raise _FallBack("handshake failed", f"stream request: {e.reason}") from e
            except TokenError as e:
                raise _FallBack("handshake failed", f"stream: {e}") from e
            except HandshakeError as e:
                raise _FallBack("handshake failed", str(e)) from e
            except StreamSilent as e:
                raise _FallBack(f"no messages within {STREAM_SILENT_S:.0f} s", str(e)) from e
            except StreamDropped as e:
                n = self._dropped(self.clock())
                self._fail(str(e))
                if n >= DROP_LIMIT:
                    raise _FallBack(f"{n} drops in {DROP_WINDOW_S / 60:.0f} min", str(e)) from e
                await self.sleep(min(BACKOFF_MAX_S, 2.0 ** (n - 1)))
            finally:
                conn.open = False

    async def _stream_plan(self, w: Wanted) -> tuple[dict[Kind, tuple[int, ...]], Callable[[Kind, list[dict]], None]]:
        """({kind: ids} of the kinds `w` needs, the dispatcher of a message's quotes per kind)."""
        sids = await self._symbol_ids(w.symbols)
        prev = await self._prev_closes(sids)
        opt = await self._option_ids(w.contracts)
        sym_of = {sid: s for s, sid in sids.items()}
        subs: dict[Kind, tuple[int, ...]] = {}
        if sym_of:
            subs["equity"] = tuple(sorted(sym_of))
        if opt:
            subs["option"] = tuple(sorted(opt))

        def on_quotes(kind: Kind, quotes: list[dict]) -> None:
            if kind == "equity":
                self._equity(quotes, sym_of, prev)
            else:
                self._options(quotes, opt)
        return subs, on_quotes

    async def _stream_session(self) -> None:
        """The one stream of the wanted set while streaming is on. A changed set (after STREAM_DEBOUNCE_S) reopens it:
        the old connection is fully closed before the new stream requests and connect (Questrade allows one
        connection per session; a stream carries the ids registered by its stream requests). Returns when the mode
        changes or streaming falls back to polling."""
        self._fallback = None
        running: tuple[dict[Kind, tuple[int, ...]], asyncio.Task] | None = None

        async def end() -> None:
            nonlocal running
            if running is not None:
                task = running[1]
                running = None
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)     # the socket is closed when this returns
            self._conn = None
        try:
            while self.streaming():
                w = self._wanted
                self._changed.clear()
                if w.empty:
                    await end()
                    await self._wait_change(5.0)
                    continue
                try:
                    subs, on_quotes = await self._stream_plan(w)
                except (QuestradeError, TokenError) as e:
                    if isinstance(e, QuestradeError) and e.kind == "auth":
                        raise
                    self._fail(getattr(e, "reason", str(e)))
                    await self.sleep(5.0)
                    continue
                if running is not None and running[0] != subs:
                    await end()
                if not subs:
                    await self._wait_change(30.0)
                    continue
                if running is None:
                    self._conn = Conn({k: len(ids) for k, ids in subs.items()})
                    running = (subs, asyncio.create_task(self._stream_forever(self._conn, subs, on_quotes),
                                                         name="live:stream"))
                changed = asyncio.create_task(self._changed.wait())
                done, _ = await asyncio.wait([running[1], changed], return_when=asyncio.FIRST_COMPLETED)
                if changed in done:
                    await self.sleep(STREAM_DEBOUNCE_S)
                    continue
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
                e = running[1].exception()
                running = None
                self._conn = None
                if isinstance(e, _FallBack):
                    self._fall_back(e)
                    return
                if e is not None:
                    raise e
        finally:
            await end()
