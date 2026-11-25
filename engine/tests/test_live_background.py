"""Background symbols (live/background.py, docs/INTEGRATION.md §9): the followed symbols' composition, cap and
priority, and the hub keeping them live (equity quotes only, per market session, only while Questrade is usable) so
that 1-minute bars are built for a symbol no window shows, without patching any window."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from optionlens import db, live
from optionlens.live import background as bg
from optionlens.live.hub import LiveHub
from optionlens.live.subscriptions import EMPTY, Wanted
from optionlens.portfolio.model import Holding
from optionlens.repo import user as user_repo
from optionlens.repo import views as views_repo
from optionlens.services import portfolio

from .test_live import C1, Bridge, Clock, FakeDriver, W


def make_hub(sessions=None, driver=None, bridge=None, background=None):
    sessions = sessions or {"US": "regular", "CA": "regular"}
    clock = Clock()
    h = LiveHub(lambda: driver, bridge or Bridge(), None, clock=clock,
                session=lambda _ts, market="US": sessions[market], background=background)
    return h, clock


# ----------------------------------------------------------------------------- composition (pure)
def test_compose_orders_portfolio_then_watchlists_then_recent_once_each_and_caps():
    assert bg.compose(["AAPL", "RY.TO"], ["MSFT", "AAPL"], ["NVDA", "MSFT", "TSLA"]) == (
        "AAPL", "RY.TO", "MSFT", "NVDA", "TSLA")
    recent = [f"R{i}" for i in range(bg.RECENT_MAX + 5)]
    assert bg.compose([], [], recent) == tuple(recent[:bg.RECENT_MAX])           # only the most recent few
    held = [f"P{i}" for i in range(30)]
    watch = [f"W{i}" for i in range(30)]
    out = bg.compose(held, watch, ["X"])
    assert len(out) == bg.BACKGROUND_MAX and out[:30] == tuple(held) and "X" not in out   # cap: portfolio first
    assert bg.compose(held, watch, [], cap=5) == tuple(held[:5])


def test_held_underlyings_largest_position_first(monkeypatch):
    def h(sym, und, kind, qty, cost, mult=1.0):
        return Holding("questrade", "1", "x", kind, sym, und, "USD", qty, cost, mult)
    monkeypatch.setattr(portfolio, "_held", lambda: ([h("AAPL", "AAPL", "stock", 10, 200.0),
                                                      h("X", "SPY", "option", -2, 5.0, 100.0),
                                                      h("RY.TO", "RY.TO", "stock", 100, 150.0),
                                                      h("CASH", "CASH", "other", 1, 1e9)], [], []))
    assert portfolio.held_underlyings() == ["RY.TO", "AAPL", "SPY"]


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    yield
    db.close()


def test_followed_reads_holdings_watchlists_and_recent_views(tmpdb, monkeypatch):
    monkeypatch.setattr(portfolio, "held_underlyings", lambda: ["SPY"])
    user_repo.create_watchlist("Main", ["MSFT", "SPY"])
    now = datetime.now(timezone.utc)
    views_repo.record_view("OLD", now - bg.RECENT_WINDOW - timedelta(hours=1))   # opened too long ago
    views_repo.record_view("NVDA", now - timedelta(hours=2))
    views_repo.record_view("TSLA", now - timedelta(minutes=5))
    assert live._followed() == ("SPY", "MSFT", "TSLA", "NVDA")


# ----------------------------------------------------------------------------- the hub
def test_background_symbols_are_kept_live_without_a_window_as_equity_quotes_only():
    d = FakeDriver()
    h, _ = make_hub(driver=d)
    h.set_background(["AAPL", "MSFT"])
    h.reconcile()
    assert d.wanted == W(["AAPL", "MSFT"]) and not d.wanted.contracts
    assert h.status()["background"] == 2 and h.status()["symbols"] == 0 and h.status()["state"] == "idle"
    h.subscribe("win-aaaa1", W(["AAPL"], [C1]))                                  # a window shows one of them
    assert d.wanted == Wanted(frozenset({"AAPL", "MSFT"}), {C1: "XYZ"})
    assert h.status()["background"] == 1 and h.status()["symbols"] == 1        # counted once, as the window's
    h.set_background([f"S{i}" for i in range(bg.BACKGROUND_MAX + 10)])
    h.reconcile()
    assert h.status()["background"] == bg.BACKGROUND_MAX                        # capped (AAPL now the window's only)


def test_background_follows_each_markets_session_and_never_runs_outside_them():
    d = FakeDriver()
    sessions = {"US": "closed", "CA": "regular"}                                # e.g. US Thanksgiving, TSX open
    h, _ = make_hub(sessions=sessions, driver=d)
    h.set_background(["AAPL", "RY.TO"])
    h.reconcile()
    assert d.wanted == W(["RY.TO"])
    sessions.update(US="extended", CA="closed")
    h.reconcile()
    assert d.wanted == W(["AAPL"])
    sessions.update(US="closed")
    h.reconcile()
    assert d.wanted == EMPTY and h.status()["background"] == 0


def test_background_needs_a_usable_questrade_and_is_never_asked_of_the_bridge():
    d = FakeDriver("not connected")
    h, _ = make_hub(driver=d, bridge=Bridge(True))
    h.set_background(["AAPL", "MSFT"])
    h.subscribe("win-aaaa1", W(["SPY"]))
    assert d.wanted == EMPTY and h.status()["background"] == 0
    assert h.active == "ibkr" and h.bridge_wants()["symbols"] == ["SPY"]       # the windows' symbols only
    h, _ = make_hub(driver=None)
    h.set_background(["AAPL"])
    h.reconcile()
    assert h.status()["background"] == 0


async def test_the_driver_runs_for_the_background_alone_and_stops_without_it():
    d = FakeDriver()
    h, _ = make_hub(driver=d)
    await h.start()
    try:
        h.set_background(["AAPL"])
        h.reconcile()
        await asyncio.sleep(0)
        assert d.ran == 1 and h._qtask is not None
        h.set_background([])
        h.reconcile()
        assert h._qtask is None and d.wanted == EMPTY
    finally:
        await h.stop()


async def test_the_hub_reads_the_background_set_from_the_injected_reader():
    d = FakeDriver()
    h, _ = make_hub(driver=d, background=lambda: ["AAPL", "MSFT"])
    await h.start()
    try:
        for _ in range(50):
            if d.wanted.symbols:
                break
            await asyncio.sleep(0.01)
        assert d.wanted == W(["AAPL", "MSFT"])
    finally:
        await h.stop()


def test_bars_are_built_for_a_background_symbol_no_window_shows_and_no_window_is_patched(monkeypatch):
    d = FakeDriver()
    h, clock = make_hub(driver=d)
    monkeypatch.setattr("optionlens.live.bars.intraday.in_sessions", lambda _ts, _cal: True)
    h.set_background(["AAPL"])
    h.subscribe("win-aaaa1", W(["SPY"]))
    for price, vol, dt in ((200.0, 5000, 0), (200.4, 5100, 1), (200.1, 5200, 2)):
        clock.t = 1_800_000_000.0 - 1_800_000_000.0 % 60 + dt
        h.publish("questrade", {"AAPL": {"price": price, "volume": vol, "delayed": False}}, {})
    h.reconcile()                                                                # kept: not forgotten
    (b,) = h.pending_bars("AAPL")
    assert (b.open, b.high, b.low, b.close, b.volume) == (200.4, 200.4, 200.1, 200.1, 200)
    h.tick()
    assert h._clients["win-aaaa1"].quotes == {}                                 # patches only to subscribers
    h.set_background([])
    clock.t += 120
    h.bars.take_closed(clock.t)
    h.reconcile()
    assert h.pending_bars("AAPL") == [] and "AAPL" not in h.bars._syms           # forgotten once handed over
