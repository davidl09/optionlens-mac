"""LiveHub: the engine's live-quote subsystem behind a narrow contract (docs/INTEGRATION.md §9).

- `subscribe(client_id, wanted)`: a renderer window's visible set, replacing its previous one (Registry);
- `events(client_id)`: that window's push channel (Server-Sent Event frames): `status` and coalesced `patch` events
  holding only the keys the window subscribed to;
- `publish(...)`: a driver's quotes. The Questrade driver (live/questrade.py) runs while it is usable and keeps exactly
  the union of all windows' sets live; when Questrade is unavailable (or failing) and the IBKR bridge is connected, the
  bridge is asked for snapshots of the visible symbols instead (`bridge_wants`, its pushes arrive via
  `ingested_quotes`); outside market hours nothing is fetched (`market_closed`);
- updates are coalesced: each key reaches a window at most once per TICK_S (4 per second);
- the latest live values are kept in memory and persisted every PERSIST_S (quotes_latest field-wise, the served
  option_latest rows' market fields) by the injected `persist`, so non-live views stay consistent; never archives.

No I/O here: the drivers, the bridge check, the clock, the session calendar and persistence are injected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping, Protocol

from .session import Session, next_live_start, session_at
from .subscriptions import EMPTY, Registry, Wanted, diff, for_session

log = logging.getLogger("optionlens.live")

TICK_S = 0.25            # coalescing window: at most 4 updates per second per key to a window
CONTROL_S = 1.0          # driver selection / status re-check
PERSIST_S = 60.0         # live values -> quotes_latest / option_latest
CLIENT_TTL_S = 30.0      # a window without an open stream this long is dropped (its set no longer kept live)
KEEPALIVE_S = 15.0       # SSE comment when nothing else was sent
FRESH_S = 30.0           # values younger than this are sent as a snapshot to a window that newly subscribes to them
STALE_MIN_S = 10.0       # live data older than max(this, 3 x the poll interval) is stale
BRIDGE_STALE_S = 30.0    # IBKR fallback: no snapshot pushed this long -> stale
FAILING_S = 30.0         # Questrade failing this long (no success) -> fall back to IBKR when it is connected
BRIDGE_INTERVAL_S = 5.0  # how often the bridge is asked to snapshot the visible symbols
MAX_BRIDGE_SYMBOLS = 50

State = Literal["live", "delayed", "connecting", "stale", "market_closed", "not_connected", "idle"]
Source = Literal["questrade", "ibkr"]
Fields = dict[str, Any]


class Driver(Protocol):
    """A live source the hub runs while it is the active one (live/questrade.py)."""
    name: str

    def usable(self) -> str | None:
        """Why it cannot fetch now (not connected, needs attention), or None."""
        ...

    def set_wanted(self, w: Wanted) -> None: ...

    def set_mode(self, mode: str) -> None:
        """The user's live mode ("stream" | "poll"), in effect at once."""
        ...

    def health(self) -> dict:
        """{ok_at: float | None (last success), error: str | None, mode: str, interval_s: float | None,
        fallback: str | None (why it polls although streaming is chosen)}"""
        ...

    def diagnostics(self) -> dict:
        """Details for Settings and the status endpoint (live/questrade.py)."""
        ...

    async def run(self) -> None:
        """Keep the wanted set live until cancelled."""
        ...


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@dataclass
class Client:
    id: str
    wanted: Wanted = EMPTY
    quotes: dict[str, Fields] = field(default_factory=dict)     # pending patches, merged
    options: dict[str, Fields] = field(default_factory=dict)
    status: dict | None = None                                  # pending status event
    event: asyncio.Event = field(default_factory=asyncio.Event)
    streams: int = 0
    gone_at: float | None = None                                # last stream closed (or never opened) at


def _merge(into: dict[str, Fields], key: str, fields: Fields) -> None:
    cur = into.get(key)
    into[key] = fields if cur is None else {**cur, **fields}


class LiveHub:
    def __init__(self, questrade: Callable[[], Driver | None], bridge_connected: Callable[[], bool],
                 persist: Callable[[list[Fields], list[Fields]], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time, session: Callable[[datetime], Session] = session_at):
        self._questrade_factory, self._bridge_connected = questrade, bridge_connected
        self._persist, self.clock, self._session_at = persist, clock, session
        self._questrade: Driver | None = None
        self._qtask: asyncio.Task | None = None
        self.registry = Registry()
        self._clients: dict[str, Client] = {}
        self._latest_q: dict[str, Fields] = {}       # key -> fields incl. source, delayed, ts (epoch: _at)
        self._latest_o: dict[str, Fields] = {}
        self._pending_q: dict[str, Fields] = {}
        self._pending_o: dict[str, Fields] = {}
        self._dirty_q: dict[str, Fields] = {}
        self._dirty_o: dict[str, Fields] = {}
        self._last_data: dict[str, float] = {}
        self._delayed: dict[str, bool] = {}
        self.active: Source | None = None
        self.session: Session = "closed"
        self._status: dict = {}
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._closing = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._tasks:
            return
        self._loop = asyncio.get_running_loop()
        self.reconcile()
        self._tasks = [asyncio.create_task(self._control(), name="live:control"),
                       asyncio.create_task(self._ticker(), name="live:tick"),
                       asyncio.create_task(self._persister(), name="live:persist")]

    def close_streams(self) -> None:
        """End every open stream (engine shutdown: uvicorn waits for open responses before it stops)."""
        self._closing = True
        for c in self._clients.values():
            c.event.set()

    async def stop(self) -> None:
        tasks = [*self._tasks, *([self._qtask] if self._qtask else [])]
        self._tasks, self._qtask = [], None
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush()
        self._loop = None

    # ------------------------------------------------------------------ the renderer's side
    def subscribe(self, client_id: str, wanted: Wanted) -> dict:
        """Replace the window's set. Fresh values of keys it newly asks for are queued at once (a snapshot)."""
        c = self._client(client_id)
        d = diff(c.wanted, wanted)
        c.wanted = wanted
        now = self.clock()
        for k in d.added_symbols:
            if (v := self._latest_q.get(k)) and now - v["_at"] <= FRESH_S:
                _merge(c.quotes, k, self._public(v))
        for k in d.added_contracts:
            if (v := self._latest_o.get(k)) and now - v["_at"] <= FRESH_S:
                _merge(c.options, k, self._public(v))
        if c.quotes or c.options:
            c.event.set()
        if self.registry.set(client_id, wanted):
            self._wake.set()
            self.reconcile()
        return {"client_id": client_id, "symbols": len(wanted.symbols), "contracts": len(wanted.contracts),
                "status": self._status}

    async def events(self, client_id: str) -> AsyncIterator[str]:
        """The window's SSE frames until it disconnects: the current status, then status changes and patches."""
        c = self._client(client_id)
        c.streams += 1
        c.gone_at = None
        c.status = None                     # the current one is sent first
        try:
            if self._closing:
                return
            yield sse("status", self._status)
            while not self._closing:
                try:
                    await asyncio.wait_for(c.event.wait(), KEEPALIVE_S)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                c.event.clear()
                if self._closing:
                    return
                if c.status is not None:
                    status, c.status = c.status, None
                    yield sse("status", status)
                if c.quotes or c.options:
                    quotes, options = c.quotes, c.options
                    c.quotes, c.options = {}, {}
                    yield sse("patch", {"quotes": quotes, "options": options})
        finally:
            c.streams -= 1
            if c.streams == 0:
                c.gone_at = self.clock()

    def status(self) -> dict:
        return self._status

    def snapshot(self, symbols: list[str] | None = None, contracts: list[str] | None = None) -> dict:
        """The latest live values in memory ({status, quotes: {SYM: fields}, options: {OSI: fields}}, each with its
        `ts`): of the given keys, or of every key that has one. Only what some window kept live has values."""
        def pick(latest: dict[str, Fields], keys: list[str] | None) -> dict[str, Fields]:
            ks = latest.keys() if keys is None else [k for k in keys if k in latest]
            return {k: self._public(latest[k]) for k in ks}
        return {"status": self._status, "quotes": pick(self._latest_q, symbols),
                "options": pick(self._latest_o, contracts)}

    def set_mode(self, mode: str) -> None:
        """The user switched the Questrade live mode: passed to the driver at once (when there is one)."""
        if (d := self._driver()) is not None:
            d.set_mode(mode)
        self._wake.set()

    def diagnostics(self) -> dict | None:
        """The Questrade driver's details (streams, messages/s, fallback), or None without a driver."""
        d = self._driver()
        return d.diagnostics() if d is not None else None

    # ------------------------------------------------------------------ the sources' side
    def publish(self, source: Source, quotes: Mapping[str, Fields], options: Mapping[str, Fields],
                persist: bool = True) -> None:
        """A driver's quotes (event loop). Only changed values are passed on; every value counts as fresh data."""
        now = self.clock()
        self._last_data[source] = now
        if quotes or options:
            self._delayed[source] = any(bool(f.get("delayed")) for f in (*quotes.values(), *options.values()))
        for latest, pending, dirty, items in ((self._latest_q, self._pending_q, self._dirty_q, quotes),
                                              (self._latest_o, self._pending_o, self._dirty_o, options)):
            for k, f in items.items():
                f = {k2: v for k2, v in f.items() if v is not None}
                old = latest.get(k)
                entry = {**(old or {}), **f, "source": source, "_at": now}
                latest[k] = entry
                if old is not None and all(old.get(k2) == v for k2, v in f.items()) and old.get("source") == source:
                    continue      # unchanged: nothing to show or store
                _merge(pending, k, {**f, "source": source, "ts": iso(now)})
                if persist:
                    _merge(dirty, k, {**f, "source": source, "_at": now})

    def ingested_quotes(self, source: str, delayed: bool | None, rows: list[Mapping]) -> None:
        """Quotes pushed through the ingest API (any thread). While the IBKR fallback is active they are live
        patches (already stored by the ingest itself)."""
        loop = self._loop
        if loop is None or source != "ibkr":
            return
        patches = {r["symbol"]: {k: r.get(k) for k in ("price", "change", "change_pct", "bid", "ask", "open", "high",
                                                     "low", "prev_close", "volume")} | {"delayed": bool(delayed)}
                   for r in rows}

        def apply() -> None:
            if self.active == "ibkr":
                wanted = self.registry.union().symbols
                self.publish("ibkr", {k: v for k, v in patches.items() if k in wanted}, {}, persist=False)
        loop.call_soon_threadsafe(apply)

    def bridge_wants(self) -> dict:
        """What the IBKR bridge should snapshot now: the visible symbols while it is the live fallback, else none."""
        syms = sorted(for_session(self.registry.union(), self.session).symbols) if self.active == "ibkr" else []
        return {"symbols": syms[:MAX_BRIDGE_SYMBOLS], "interval_s": BRIDGE_INTERVAL_S}

    # ------------------------------------------------------------------ control
    def _client(self, client_id: str) -> Client:
        c = self._clients.get(client_id)
        if c is None:
            c = self._clients[client_id] = Client(client_id, gone_at=self.clock())
        return c

    def _expire_clients(self, now: float) -> None:
        for cid, c in list(self._clients.items()):
            if c.streams == 0 and c.gone_at is not None and now - c.gone_at > CLIENT_TTL_S:
                del self._clients[cid]
                self.registry.drop(cid)

    def _driver(self) -> Driver | None:
        if self._questrade is None:
            self._questrade = self._questrade_factory()
        return self._questrade

    def _questrade_failing(self, d: Driver, now: float) -> bool:
        h = d.health()
        return h.get("error") is not None and (h.get("ok_at") is None or now - h["ok_at"] > FAILING_S)

    def reconcile(self) -> None:
        """Pick the active source for the current session and wanted set, run / stop the Questrade driver, and
        broadcast the status when it changed."""
        now = self.clock()
        self._expire_clients(now)
        self.session = self._session_at(datetime.fromtimestamp(now, timezone.utc))
        want = for_session(self.registry.union(), self.session)
        q = self._driver()
        q_reason = q.usable() if q is not None else "not configured"
        active: Source | None = None
        if not want.empty:
            if q is not None and q_reason is None and not (self._questrade_failing(q, now) and self._bridge_connected()):
                active = "questrade"
            elif self._bridge_connected():
                active = "ibkr"
        run_q = q is not None and q_reason is None and not want.empty
        if q is not None:
            q.set_wanted(want if run_q else EMPTY)
        if run_q and self._qtask is None and self._loop is not None:
            self._qtask = asyncio.create_task(self._run_driver(q), name="live:questrade")  # type: ignore[arg-type]
        elif not run_q and self._qtask is not None:
            self._qtask.cancel()
            self._qtask = None
        if active != self.active:
            log.info("live: source %s -> %s", self.active, active)
        self.active = active
        self._set_status(self._compute_status(now, want, q, q_reason))

    async def _run_driver(self, d: Driver) -> None:
        try:
            await d.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a driver bug must not kill the hub; reconcile restarts it
            log.exception("live: the Questrade driver stopped")
        finally:
            if self._qtask is asyncio.current_task():
                self._qtask = None

    def _compute_status(self, now: float, want: Wanted, q: Driver | None, q_reason: str | None) -> dict:
        union = self.registry.union()
        st: dict[str, Any] = {"session": self.session, "source": self.active, "mode": None, "interval_s": None,
                              "fallback": None, "symbols": len(want.symbols), "contracts": len(want.contracts),
                              "detail": None}
        if self.session == "closed":
            nxt = next_live_start(datetime.fromtimestamp(now, timezone.utc))
            return {**st, "state": "market_closed", "detail": f"US markets are closed: showing the last stored values. "
                    f"Live quotes resume {nxt:%a %H:%M} ET."}
        if union.empty:
            return {**st, "state": "idle"}
        if self.session == "extended" and want.empty:
            return {**st, "state": "market_closed", "detail": "Options trade 09:30-16:00 ET: showing the last stored "
                    "option quotes."}
        if self.active is None:
            why = q_reason or "unavailable"
            return {**st, "state": "not_connected",
                    "detail": f"No live source: Questrade {why}; IB Gateway not connected."}
        if self.active == "questrade" and q is not None:
            h = q.health()
            st |= {"mode": h.get("mode"), "interval_s": h.get("interval_s"), "fallback": h.get("fallback")}
            ok_at, stale_after = h.get("ok_at"), max(STALE_MIN_S, 3 * (h.get("interval_s") or 0))
            if ok_at is None:
                return {**st, "state": "stale" if h.get("error") else "connecting", "detail": h.get("error")}
            if now - ok_at > stale_after:
                return {**st, "state": "stale", "detail": h.get("error") or "no update from Questrade recently"}
        else:
            st |= {"mode": "snapshots", "interval_s": BRIDGE_INTERVAL_S,
                   "detail": "Questrade unavailable: IBKR snapshots of the visible symbols (option quotes from the "
                             "last stored chain)."}
            at = self._last_data.get("ibkr")
            if at is None:
                return {**st, "state": "connecting"}
            if now - at > BRIDGE_STALE_S:
                return {**st, "state": "stale"}
        partial = self.session == "extended"
        state: State = "delayed" if self._delayed.get(self.active) else "live"
        if partial and st["detail"] is None:
            st["detail"] = "Extended hours: stock quotes only."
        return {**st, "state": state}

    def _set_status(self, st: dict) -> None:
        if st == self._status:
            return
        self._status = st
        for c in self._clients.values():
            c.status = st
            c.event.set()

    async def _control(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), CONTROL_S)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001
                log.exception("live: reconcile failed")

    def tick(self) -> None:
        """Hand the pending (coalesced) patches to the windows that subscribed to them."""
        if not self._pending_q and not self._pending_o:
            return
        pq, po = self._pending_q, self._pending_o
        self._pending_q, self._pending_o = {}, {}
        for c in self._clients.values():
            hit = False
            for k in pq.keys() & c.wanted.symbols:
                _merge(c.quotes, k, pq[k])
                hit = True
            for k in po.keys() & c.wanted.contracts.keys():
                _merge(c.options, k, po[k])
                hit = True
            if hit:
                c.event.set()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self.tick()

    async def flush(self) -> None:
        """Persist the live values changed since the last flush."""
        if self._persist is None or (not self._dirty_q and not self._dirty_o):
            return
        dq, do = self._dirty_q, self._dirty_o
        self._dirty_q, self._dirty_o = {}, {}
        try:
            await self._persist([{"symbol": k, **v} for k, v in dq.items()], [{"osi": k, **v} for k, v in do.items()])
        except Exception:  # noqa: BLE001 - the next flush tries again with newer values
            log.exception("live: persisting live quotes failed")

    async def _persister(self) -> None:
        while True:
            await asyncio.sleep(PERSIST_S)
            await self.flush()

    @staticmethod
    def _public(v: Fields) -> Fields:
        return {**{k: x for k, x in v.items() if k != "_at"}, "ts": iso(v["_at"])}
