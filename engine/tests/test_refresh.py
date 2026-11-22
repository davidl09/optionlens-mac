"""On-demand refresh rounds (services/refresh.py, docs/INTEGRATION.md §7): every connected chain source at once, one
source-neutral status per symbol, the IBKR bridge reporting its own outcome."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from optionlens.jobs import prices
from optionlens.repo import bridge as bridge_repo
from optionlens.services import refresh
from optionlens.sources import chain_source
from optionlens.sources.questrade.errors import QuestradeError

from .test_chain_sources import FRI, ny, payload, snap, tmpdb  # noqa: F401  (fixtures)


class Src:
    name = "questrade"

    def __init__(self, payload):
        self.payload, self.calls = payload, []
        self.is_connected, self.why, self.fail = True, None, None
        self.gate = asyncio.Event()
        self.gate.set()

    def connected(self):
        return self.is_connected

    def unavailable(self):
        return self.why

    def problem(self):
        return None

    def status(self):
        return {}

    async def keepalive(self):
        return None

    async def fetch_chain(self, symbol, final=False):
        self.calls.append(symbol)
        await self.gate.wait()
        if self.fail:
            raise self.fail
        return dataclasses.replace(snap(self.payload, datetime.now(timezone.utc)), delayed=False)


@pytest.fixture
def src(tmpdb, payload, monkeypatch):
    s = Src(payload)
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: s)

    async def no_history(row, start=None):
        return 0
    monkeypatch.setattr(prices, "backfill_symbol", no_history)
    return s


def bridge_connected():
    bridge_repo.record_heartbeat("optionlens-mac test", True, "live", datetime.now(timezone.utc))


async def settle():
    await asyncio.gather(*list(refresh._tasks))


def states(st):
    return {s["name"]: s["state"] for s in st["sources"]}


async def test_all_connected_sources_in_parallel_first_chain_served_bridge_reports(src):
    bridge_connected()
    src.gate.clear()
    st = await refresh.start("AAPL")
    assert st["pending"] and states(st) == {"ibkr": "pending", "questrade": "pending"} and st["served"] is None
    src.gate.set()
    await settle()
    st = refresh.status("AAPL")
    assert states(st) == {"ibkr": "pending", "questrade": "ok"} and st["pending"]
    assert st["served"]["source"] == "questrade" and st["served"]["delayed"] is False      # first chain at once
    st = refresh.report("AAPL", "ibkr", "unavailable", "IBKR's contract server is unavailable", "contract-server")
    assert not st["pending"] and states(st) == {"ibkr": "unavailable", "questrade": "ok"}
    ib = next(s for s in st["sources"] if s["name"] == "ibkr")
    assert ib["waiting_for"] == "contract-server" and "contract server" in ib["message"]


async def test_a_pending_round_is_joined_and_a_fresh_source_not_asked_again(src):
    src.gate.clear()
    await asyncio.gather(refresh.start("AAPL"), refresh.start("AAPL"))
    src.gate.set()
    await settle()
    assert src.calls == ["AAPL"]
    st = await refresh.start("AAPL")                                   # Retry within FRESH_S: no new fetch
    assert states(st) == {"questrade": "ok"} and not st["pending"] and src.calls == ["AAPL"]


async def test_sources_not_set_up_are_left_out_unusable_ones_say_why(src):
    src.is_connected = False
    st = await refresh.start("AAPL")
    assert st["sources"] == [] and not st["pending"]                   # no source connected at all
    src.is_connected, src.why = True, "Your Questrade app lacks the market-data permission"
    refresh._rounds.clear()
    st = await refresh.start("AAPL")
    assert states(st) == {"questrade": "failed"} and st["sources"][0]["message"] == src.why and src.calls == []


async def test_a_failed_fetch_reports_the_classified_reason(src):
    src.fail = QuestradeError("u", 403, 1016, "Request is out of allowed OAuth scopes")
    await refresh.start("AAPL")
    await settle()
    s = refresh.status("AAPL")["sources"][0]
    assert s["state"] == "failed" and "Retrieve delayed and real-time market data" in s["message"]
    refresh._rounds.clear()
    src.fail = QuestradeError("u", 404, 1019, "Symbol not found.")
    await refresh.start("AAPL")
    await settle()
    assert refresh.status("AAPL")["sources"][0]["message"] == "404 code 1019 (symbol not found)"


async def test_a_bridge_that_never_reports_times_out(src, monkeypatch):
    bridge_connected()
    src.is_connected = False
    await refresh.start("AAPL")
    r = refresh._rounds["AAPL"]
    r.attempts["ibkr"].at -= timedelta(seconds=refresh.BRIDGE_TIMEOUT_S + 1)
    st = refresh.status("AAPL")
    assert states(st) == {"ibkr": "failed"} and "did not answer" in st["sources"][0]["message"]


def test_a_report_without_a_round_starts_one(tmpdb):
    st = refresh.report("AAPL", "ibkr", "refreshed")
    assert states(st) == {"ibkr": "ok"} and not st["pending"]
    assert refresh.report("AAPL", "ibkr", "no-options")["sources"][0]["state"] == "no_options"
