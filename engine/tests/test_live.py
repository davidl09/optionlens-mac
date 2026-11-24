"""Live quotes (live/*, docs/INTEGRATION.md §9): subscriptions and their diffing, sessions, the hub (coalescing,
snapshots, status, driver selection, IBKR fallback, client expiry, persistence throttle, SSE frames) and the Questrade
driver's polling (cadence vs the rate gate, the calls it makes, the patches it publishes)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime

import pytest

from optionlens.analytics.calendar import NY
import optionlens.live.hub as hub_mod
from optionlens.live import questrade as qlive
from optionlens.live.hub import LiveHub
from optionlens.live.session import next_live_start, session_at
from optionlens.live.subscriptions import EMPTY, Registry, Wanted, diff, for_session, union
from optionlens.sources.questrade import source as source_mod
from optionlens.sources.questrade.ratelimit import RateGate
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade, option_id

C1, C2, C3 = "XYZ261120C00100000", "XYZ261120P00100000", "XYZ261127C00110000"


def W(symbols=(), contracts=()) -> Wanted:
    return Wanted(frozenset(symbols), {c: "XYZ" for c in contracts})


# ----------------------------------------------------------------------------- subscriptions
def test_union_diff_and_registry_replace_a_clients_set():
    r = Registry()
    assert r.set("a", W(["AAPL", "SPY"], [C1])) is True
    assert r.set("b", W(["SPY"], [C2])) is True
    assert r.union() == W(["AAPL", "SPY"], [C1, C2])
    assert r.set("b", W(["SPY"], [C2])) is False               # same set again: union unchanged
    assert r.set("a", W(["SPY"])) is True                      # replaces a's previous set (AAPL, C1 gone)
    assert r.union() == W(["SPY"], [C2])
    d = diff(W(["AAPL", "SPY"], [C1]), W(["SPY", "QQQ"], [C2]))
    assert (d.added_symbols, d.removed_symbols) == ({"QQQ"}, {"AAPL"})
    assert (d.added_contracts, d.removed_contracts) == ({C2}, {C1}) and not d.empty
    assert diff(W(["A"]), W(["A"])).empty
    assert r.drop("b") is True and r.union() == W(["SPY"]) and r.drop("zzz") is False
    assert union([]) == EMPTY


def test_for_session_drops_options_outside_the_regular_session():
    w = W(["AAPL"], [C1])
    assert for_session(w, "regular") == w
    assert for_session(w, "extended") == W(["AAPL"])
    assert for_session(w, "closed") == EMPTY


def test_sessions():
    at = lambda *a: datetime(*a, tzinfo=NY)                                      # noqa: E731
    assert session_at(at(2026, 11, 24, 9, 29)) == "extended"
    assert session_at(at(2026, 11, 24, 9, 30)) == "regular"
    assert session_at(at(2026, 11, 24, 15, 59)) == "regular"
    assert session_at(at(2026, 11, 24, 16, 0)) == "extended"
    assert session_at(at(2026, 11, 24, 20, 0)) == "closed"
    assert session_at(at(2026, 11, 24, 3, 59)) == "closed"
    assert session_at(at(2026, 11, 27, 13, 30)) == "extended"                   # day after Thanksgiving: 13:00 close
    assert session_at(at(2026, 11, 26, 11, 0)) == "closed"                      # Thanksgiving
    assert session_at(at(2026, 11, 28, 11, 0)) == "closed"                      # Saturday
    assert next_live_start(at(2026, 11, 25, 21, 0)) == at(2026, 11, 27, 4, 0)   # skips Thanksgiving


# ----------------------------------------------------------------------------- hub
class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeDriver:
    name = "questrade"

    def __init__(self, reason: str | None = None):
        self.reason, self.wanted, self.ok_at, self.error, self.ran = reason, EMPTY, None, None, 0

    def usable(self):
        return self.reason

    def set_wanted(self, w):
        self.wanted = w

    def health(self):
        return {"ok_at": self.ok_at, "error": self.error, "mode": "poll", "interval_s": 2.0}

    async def run(self):
        self.ran += 1
        await asyncio.Event().wait()


class Bridge:
    def __init__(self, connected=False):
        self.connected = connected

    def __call__(self):
        return self.connected


def make_hub(session="regular", driver=None, bridge=None, persisted=None):
    clock = Clock()

    async def persist(q, o):
        persisted.append((q, o))
    h = LiveHub(lambda: driver, bridge or Bridge(), persist if persisted is not None else None, clock=clock,
                session=lambda _ts: session)
    return h, clock


def test_patches_are_coalesced_per_tick_and_filtered_per_window():
    d = FakeDriver()
    h, clock = make_hub(driver=d)
    h.subscribe("win-aaaa1", W(["XYZ"], [C1]))
    h.subscribe("win-bbbb2", W(["SPY"]))
    h.publish("questrade", {"XYZ": {"price": 100.0, "bid": 99.9}}, {C1: {"bid": 1.0, "ask": 1.2}})
    clock.t += 0.1
    h.publish("questrade", {"XYZ": {"price": 100.5}, "SPY": {"price": 600.0}}, {C1: {"bid": 1.1}})
    h.tick()
    a, b = h._clients["win-aaaa1"], h._clients["win-bbbb2"]
    assert set(a.quotes) == {"XYZ"} and set(b.quotes) == {"SPY"}               # each window: its keys only
    assert a.quotes["XYZ"]["price"] == 100.5 and a.quotes["XYZ"]["bid"] == 99.9   # one merged patch per key
    assert a.options[C1] == {"bid": 1.1, "ask": 1.2, "source": "questrade", "ts": a.options[C1]["ts"]}
    a.quotes.clear()
    h.publish("questrade", {"XYZ": {"price": 100.5, "bid": 99.9}}, {})       # unchanged: not passed on
    h.tick()
    assert a.quotes == {}


def test_a_window_newly_subscribing_gets_fresh_values_at_once():
    h, clock = make_hub(driver=FakeDriver())
    h.subscribe("win-aaaa1", W(["XYZ"]))
    h.publish("questrade", {"XYZ": {"price": 100.0}}, {})
    h.subscribe("win-bbbb2", W(["XYZ"]))
    assert h._clients["win-bbbb2"].quotes["XYZ"]["price"] == 100.0
    clock.t += hub_mod.FRESH_S + 1                                              # too old: no snapshot
    h.subscribe("win-cccc3", W(["XYZ"]))
    assert h._clients["win-cccc3"].quotes == {}


def test_status_states_and_driver_selection():
    d = FakeDriver()
    h, clock = make_hub(session="closed", driver=d)
    h.subscribe("win-aaaa1", W(["XYZ"], [C1]))
    assert h.status()["state"] == "market_closed" and "resume" in h.status()["detail"]
    assert d.wanted == EMPTY and h.active is None                               # nothing fetched while closed

    h, clock = make_hub(driver=d)
    h.reconcile()
    assert h.status()["state"] == "idle"
    h.subscribe("win-aaaa1", W(["XYZ"], [C1]))
    assert h.active == "questrade" and d.wanted == W(["XYZ"], [C1])
    assert h.status()["state"] == "connecting"
    d.ok_at = clock.t
    h.publish("questrade", {"XYZ": {"price": 1.0, "delayed": False}}, {})
    h.reconcile()
    assert h.status()["state"] == "live" and h.status()["source"] == "questrade"
    h.publish("questrade", {"XYZ": {"price": 1.1, "delayed": True}}, {})
    h.reconcile()
    assert h.status()["state"] == "delayed"
    clock.t += 30
    h.reconcile()
    assert h.status()["state"] == "stale"                                       # no success for 30 s

    h, _ = make_hub(session="extended", driver=FakeDriver())
    h.subscribe("win-aaaa1", W(["XYZ"], [C1]))
    assert h._questrade.wanted == W(["XYZ"])                                    # extended hours: underlyings only
    h, _ = make_hub(session="extended", driver=FakeDriver())
    h.subscribe("win-aaaa1", W([], [C1]))
    assert h.status()["state"] == "market_closed"                              # options only: not live now


def test_falls_back_to_ibkr_only_when_questrade_is_unavailable():
    h, _ = make_hub(driver=FakeDriver("not connected"), bridge=Bridge(False))
    h.subscribe("win-aaaa1", W(["XYZ", "SPY"]))
    assert h.active is None and h.status()["state"] == "not_connected"
    assert "not connected" in h.status()["detail"] and h.bridge_wants()["symbols"] == []

    bridge = Bridge(True)
    h, clock = make_hub(driver=FakeDriver("not connected"), bridge=bridge)
    h.subscribe("win-aaaa1", W(["XYZ", "SPY"], [C1]))
    assert h.active == "ibkr" and h.status()["state"] == "connecting"
    assert h.bridge_wants() == {"symbols": ["SPY", "XYZ"], "interval_s": hub_mod.BRIDGE_INTERVAL_S}
    h.publish("ibkr", {"XYZ": {"price": 100.0, "delayed": False}}, {}, persist=False)
    h.reconcile()
    assert h.status()["state"] == "live" and h.status()["mode"] == "snapshots"
    assert h._dirty_q == {}                                                      # stored by the ingest itself

    d = FakeDriver()                                                             # Questrade usable: preferred
    h, clock = make_hub(driver=d, bridge=Bridge(True))
    h.subscribe("win-aaaa1", W(["XYZ"]))
    assert h.active == "questrade" and h.bridge_wants()["symbols"] == []
    d.error, d.ok_at = "no response (ConnectTimeout)", clock.t - hub_mod.FAILING_S - 1   # failing: IBKR meanwhile
    h.reconcile()
    assert h.active == "ibkr" and d.wanted == W(["XYZ"])                        # the driver keeps retrying
    d.error, d.ok_at = None, clock.t
    h.reconcile()
    assert h.active == "questrade"


async def test_ingested_ibkr_quotes_are_patches_only_while_ibkr_is_the_fallback():
    h, _ = make_hub(driver=FakeDriver("not connected"), bridge=Bridge(True))
    await h.start()
    try:
        h.subscribe("win-aaaa1", W(["XYZ"]))
        h.ingested_quotes("ibkr", False, [{"symbol": "XYZ", "price": 101.0, "ts": "x"},
                                          {"symbol": "ZZZ", "price": 5.0, "ts": "x"}])
        await asyncio.sleep(0)
        assert set(h._pending_q) == {"XYZ"} and h._pending_q["XYZ"]["price"] == 101.0
    finally:
        await h.stop()
    h2, _ = make_hub(driver=FakeDriver(), bridge=Bridge(True))
    await h2.start()
    try:
        h2.subscribe("win-aaaa1", W(["XYZ"]))
        h2.ingested_quotes("ibkr", False, [{"symbol": "XYZ", "price": 101.0}])
        await asyncio.sleep(0)
        assert h2._pending_q == {}                                               # Questrade is the live source
    finally:
        await h2.stop()


def test_windows_without_a_stream_are_dropped_after_the_ttl():
    d = FakeDriver()
    h, clock = make_hub(driver=d)
    h.subscribe("win-aaaa1", W(["XYZ"]))
    clock.t += hub_mod.CLIENT_TTL_S + 1
    h.reconcile()
    assert h.registry.union() == EMPTY and d.wanted == EMPTY and "win-aaaa1" not in h._clients


async def test_persistence_is_throttled_and_carries_only_changes():
    persisted: list = []
    h, clock = make_hub(driver=FakeDriver(), persisted=persisted)
    h.subscribe("win-aaaa1", W(["XYZ"], [C1]))
    for i in range(10):                                                         # 10 polls within a minute
        h.publish("questrade", {"XYZ": {"price": 100 + i, "delayed": False}}, {C1: {"bid": 1.0}})
        h.tick()
        clock.t += 2
    assert persisted == []                                                      # nothing written per update
    await h.flush()
    (q, o), = persisted
    assert q == [{"symbol": "XYZ", "price": 109, "delayed": False, "source": "questrade", "_at": q[0]["_at"]}]
    assert o == [{"osi": C1, "bid": 1.0, "source": "questrade", "_at": o[0]["_at"]}]
    await h.flush()
    assert len(persisted) == 1                                                  # unchanged since: nothing to write


async def test_sse_frames_status_then_patches():
    d = FakeDriver()
    h, clock = make_hub(driver=d)
    h.subscribe("win-aaaa1", W(["XYZ"]))
    gen = h.events("win-aaaa1").__aiter__()
    first = await gen.__anext__()
    assert first.startswith("event: status\ndata: ") and json.loads(first.split("data: ")[1])["state"] == "connecting"
    h.publish("questrade", {"XYZ": {"price": 100.0}}, {})
    h.tick()
    frame = await asyncio.wait_for(gen.__anext__(), 1)
    assert frame.startswith("event: patch\n") and json.loads(frame.split("data: ")[1])["quotes"]["XYZ"]["price"] == 100.0
    d.ok_at = clock.t
    h.reconcile()
    frame = await asyncio.wait_for(gen.__anext__(), 1)
    assert json.loads(frame.split("data: ")[1])["state"] == "live"
    assert h._clients["win-aaaa1"].streams == 1
    await gen.aclose()
    assert h._clients["win-aaaa1"].streams == 0 and h._clients["win-aaaa1"].gone_at == clock.t


async def test_the_driver_task_runs_only_while_something_is_wanted():
    d = FakeDriver()
    h, _ = make_hub(driver=d)
    await h.start()
    try:
        await asyncio.sleep(0)
        assert d.ran == 0
        h.subscribe("win-aaaa1", W(["XYZ"]))
        await asyncio.sleep(0)
        assert d.ran == 1 and h._qtask is not None
        h.subscribe("win-aaaa1", EMPTY)
        assert h._qtask is None and d.wanted == EMPTY
    finally:
        await h.stop()


# ----------------------------------------------------------------------------- Questrade driver: polling
def test_cadence_keeps_live_polling_within_the_budget_left_by_the_sweeps():
    g = RateGate()
    assert qlive.cadence(1, g.hour_budget) == qlive.MIN_INTERVAL_S               # one call: every 2 s
    assert qlive.calls_per_round(1, 300) == 4                                   # 300 visible contracts + underlying
    iv = qlive.cadence(4, g.hour_budget)
    assert iv == pytest.approx(4 / ((10_500 - 6_000) / 3600))                   # 3.2 s
    for calls in (1, 2, 4, 7, 11):                                              # live calls/h + sweep reserve
        per_hour = calls * 3600 / qlive.cadence(calls, g.hour_budget)            # stay within 70% of 15,000/h
        assert per_hour + qlive.SWEEP_RESERVE_PER_HOUR <= g.hour_budget <= 0.70 * 15_000


@pytest.fixture
def qt(tmp_path, monkeypatch):
    fake = FakeQuestrade()
    src = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=fake.http)
    monkeypatch.setattr(source_mod, "now_ny", lambda: datetime(2026, 11, 19, 10, 0, tzinfo=NY))
    return fake, src


async def test_poll_round_publishes_underlyings_and_contracts(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    published = []
    d = qlive.QuestradeLive(src, lambda s, q, o: published.append((s, dict(q), dict(o))))
    calls = await d.poll_once(W(["XYZ", "NOPE"], [C1, C2, C3, "XYZ261120C00105000"]))
    assert calls == 2
    (s1, q, _), (s2, _, o) = published
    assert s1 == s2 == "questrade"
    assert q["XYZ"]["price"] == 100.2 and q["XYZ"]["prev_close"] == 98.0
    assert q["XYZ"]["change"] == pytest.approx(2.2) and q["XYZ"]["delayed"] is False
    assert set(o) == {C1, C3}                                                   # C2 unquoted, 105 strike not listed
    assert o[C1]["bid"] == 1.0 and o[C1]["mid"] == pytest.approx(1.1) and o[C1]["iv"] == pytest.approx(0.315)
    body = json.loads(next(r for r in fake.requests if r.url.path == "/v1/markets/quotes/options").content)
    assert sorted(body["optionIds"]) == sorted(option_id(*k) for k in (("2026-11-20", 100, "C"),
                                                                     ("2026-11-20", 100, "P"),
                                                                     ("2026-11-27", 110, "C")))
    n = len(fake.requests)
    await d.poll_once(W(["XYZ"], [C1]))                                         # ids, info and chain cached
    assert len(fake.requests) - n == 2


async def test_poll_loop_waits_the_cadence_and_reacts_to_a_changed_set(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    slept = []

    async def sleep(s):
        slept.append(s)
    d = qlive.QuestradeLive(src, lambda *a: None, sleep=sleep)
    d.set_wanted(W(["XYZ"]))
    task = asyncio.create_task(d.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if d.rounds:
                break
        assert d.rounds == 1 and d.interval_s == qlive.MIN_INTERVAL_S and d.health()["ok_at"] is not None
        d.set_wanted(W(["XYZ"], [C1]))                                          # polled at once, not after 2 s
        for _ in range(100):
            await asyncio.sleep(0.01)
            if d.rounds == 2:
                break
        assert d.rounds == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_poll_failure_is_reported_not_raised(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    d = qlive.QuestradeLive(src, lambda *a: None, sleep=lambda s: asyncio.sleep(0))
    d.set_wanted(W(["XYZ"]))
    fake.scope_denied = True
    await d._poll_round()
    assert d.error and "1016" in d.error and d.usable() is not None             # needs attention: the hub stops it


def test_live_patch_mapping_matches_the_sweep_mapping():
    from optionlens.sources.questrade import mapping
    fake = FakeQuestrade()
    listed = mapping.Listed(option_id("2026-11-20", 90, "C"), date(2026, 11, 20), 90.0, "C")
    p = mapping.live_option("XYZ", listed, fake.option_quote("2026-11-20", 90, "C"))
    assert p["bid"] == 11.0 and p["ask"] == 11.2 and p["oi"] == 100 and p["delayed"] is False
    q = mapping.live_quote(fake.equity_quote(), "XYZ", None, datetime(2026, 11, 19, 15, tzinfo=NY))
    assert q["price"] == 100.2 and q["change"] is None and q["volume"] == 1_000_000
