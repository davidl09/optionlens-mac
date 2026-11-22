"""Chain precedence between sources (jobs/precedence.py) through the store pipeline, IV borrowed between sources,
the bridge's closing-chain want, and the server-side final / after-hours-fill sweeps (offline)."""
from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from optionlens.jobs import bridge_plan, chains, hooks, precedence
from optionlens.jobs.precedence import Quality
from optionlens.models import ChainSnapshot
from optionlens.repo import metrics as metrics_repo
from optionlens.repo import options as options_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.services import ingest
from optionlens.sources import chain_source

from .test_chain_sources import FRI, eod_source, ny, payload, snap, tmpdb  # noqa: F401  (fixtures)

ORDER = ("ibkr", "questrade")


def q(source, final=True, realtime=True, quoted=100):
    return Quality(source, final, realtime, quoted)


# ----------------------------------------------------------------------------- the rule
def test_rule_order():
    assert precedence.replaces(None, q("questrade"), ORDER)
    assert precedence.replaces(q("ibkr", quoted=900), q("ibkr", final=False, realtime=False, quoted=1), ORDER)  # same source
    assert precedence.replaces(q("ibkr", final=False, quoted=900), q("questrade", realtime=False, quoted=1), ORDER)
    assert not precedence.replaces(q("ibkr", realtime=True, quoted=1), q("questrade", realtime=False, quoted=900), ORDER)
    assert precedence.replaces(q("ibkr", realtime=False), q("questrade", realtime=True), ORDER)
    assert precedence.replaces(q("ibkr", quoted=100), q("questrade", quoted=101), ORDER)
    assert not precedence.replaces(q("ibkr", quoted=100), q("questrade", quoted=100), ORDER)       # tie: IBKR
    assert precedence.replaces(q("questrade", quoted=100), q("ibkr", quoted=100), ORDER)
    assert precedence.replaces(q("vendor"), q("questrade"), ORDER)                                  # unlisted last
    assert precedence.replaces(q("questrade"), q("ibkr"))                                           # settings order


def test_quality_of_snapshot_and_rows(payload):
    s = dataclasses.replace(snap(payload, ny(2026, 11, 20, 16, 25)), delayed=False)
    qs = precedence.of_snapshot(s, "questrade", True)
    assert qs.realtime and qs.final and 0 < qs.quoted <= len(s.contracts)
    assert qs.quoted == sum(1 for c in s.contracts if c.ask and c.ask > 0 and options_repo.archivable(c))
    assert precedence.of_metrics_row({"source": "cboe"}) == Quality("cboe", False, False, 0)   # legacy row: unknown
    assert not precedence.of_snapshot(dataclasses.replace(s, delayed=None), "x", False).realtime


# ----------------------------------------------------------------------------- through store_chain
def _thin(s: ChainSnapshot, keep: float) -> ChainSnapshot:
    return dataclasses.replace(s, contracts=s.contracts[: int(len(s.contracts) * keep)])


def test_real_time_final_beats_delayed_ibkr_archive_and_metrics(tmpdb, payload):
    t_ib, t_qt = ny(2026, 11, 20, 16, 25), ny(2026, 11, 20, 16, 21)
    chains.store_chain(dataclasses.replace(snap(payload, t_ib), delayed=True), t_ib, final=True, source="ibkr")
    assert eod_source() == "ibkr"
    chains.store_chain(dataclasses.replace(snap(payload, t_qt, price=350.0), delayed=False), t_qt, final=True,
                       source="questrade")
    assert eod_source() == "questrade" and options_repo.eod_quality(FRI)["AAPL"]["delayed"] is False
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["source"] == "questrade" and m["chain_final"] is True and m["chain_quoted"] > 0 and m["close"] == 350.0
    assert options_repo.latest_source("AAPL") == "ibkr"                          # latest = newest chain
    from optionlens.services import symbol_options
    assert symbol_options._source("AAPL") == {"source": "ibkr"}                  # Chain.source
    later = ny(2026, 11, 20, 16, 40)
    chains.store_chain(dataclasses.replace(snap(payload, later), delayed=True), later, final=True, source="ibkr")
    assert eod_source() == "questrade" and metrics_repo.source_of(FRI, "AAPL") == "questrade"


def test_equal_quality_more_quoted_contracts_win_ties_go_to_ibkr(tmpdb, payload):
    t = ny(2026, 11, 20, 16, 25)
    full = dataclasses.replace(snap(payload, t), delayed=False)
    chains.store_chain(_thin(full, 0.5), t, final=True, source="ibkr")
    chains.store_chain(full, t, final=True, source="questrade")                 # more complete
    assert eod_source() == "questrade"
    chains.store_chain(full, t, final=True, source="ibkr")                      # as complete: IBKR first
    assert eod_source() == "ibkr" and metrics_repo.source_of(FRI, "AAPL") == "ibkr"
    chains.store_chain(full, t, final=True, source="questrade")
    assert eod_source() == "ibkr"


def test_a_closing_snapshot_beats_intraday_data_of_another_source(tmpdb, payload):
    t1, t2 = ny(2026, 11, 20, 15, 0), ny(2026, 11, 20, 16, 21)
    chains.store_chain(dataclasses.replace(snap(payload, t1), delayed=False), t1, source="ibkr")
    chains.store_chain(_thin(dataclasses.replace(snap(payload, t2, price=351.0), delayed=True), 0.3), t2, final=True,
                       source="questrade")
    assert metrics_repo.get_metrics("AAPL", FRI)["close"] == 351.0 and eod_source() == "questrade"


def test_bridge_is_not_asked_for_closing_chains_a_real_time_archive_settled(tmpdb, payload):
    symbols_repo.set_universe(["AAPL"])
    t = ny(2026, 11, 20, 16, 21)
    chains.store_chain(dataclasses.replace(snap(payload, t), delayed=True), t, final=True, source="questrade")
    assert "AAPL" in bridge_plan.eod_chains_want(ny(2026, 11, 20, 17, 0))["symbols"]   # IBKR could still win
    chains.store_chain(dataclasses.replace(snap(payload, t), delayed=False), t, final=True, source="questrade")
    assert "AAPL" not in bridge_plan.eod_chains_want(ny(2026, 11, 20, 17, 0))["symbols"]


# ----------------------------------------------------------------------------- IV across sources
def test_iv_is_borrowed_from_another_source_before_it_is_computed(tmpdb, payload):
    t1, t2 = ny(2026, 11, 20, 14, 0), ny(2026, 11, 20, 15, 0)
    qt = ingest.complete_chain(dataclasses.replace(snap(payload, t1), delayed=False), "questrade")
    chains.store_chain(qt, t1, source="questrade")
    with_iv = {c.osi: c for c in qt.contracts if c.iv_source == "questrade"}
    assert with_iv
    bare = snap(payload, t2)
    bare = dataclasses.replace(bare, contracts=tuple(dataclasses.replace(c, iv=None, delta=None, gamma=None, theta=None,
                                                                         vega=None) for c in bare.contracts))
    out = {c.osi: c for c in ingest.complete_chain(bare, "ibkr").contracts}
    osi = next(iter(with_iv))
    assert out[osi].iv == with_iv[osi].iv and out[osi].iv_source == "questrade"
    assert out[osi].delta == with_iv[osi].delta and out[osi].greeks_source == "questrade"
    others = [c for o, c in out.items() if o not in with_iv and c.iv is not None]
    assert others and all(c.iv_source.startswith("computed") for c in others)   # only where neither has IV
    # a later IBKR chain with its own IV keeps it
    own = ingest.complete_chain(snap(payload, t2), "ibkr").contracts
    assert {c.iv_source for c in own if c.iv is not None and c.osi in with_iv} == {"ibkr"}


# ----------------------------------------------------------------------------- server-side sweeps
class Source:
    """A connected server-side source serving the recorded AAPL chain as real-time data."""
    name = "questrade"

    def __init__(self, payload, when):
        self.payload, self.when, self.calls, self.kept = payload, when, [], 0

    def unavailable(self):
        return None

    async def keepalive(self):
        self.kept += 1

    def status(self):
        return {}

    async def fetch_chain(self, symbol, final=False):
        self.calls.append((symbol, final))
        if symbol != "AAPL":
            raise chain_source.NotListed(symbol)
        return dataclasses.replace(snap(self.payload, self.when), delayed=False)


@pytest.fixture
def server(tmpdb, payload, monkeypatch):
    symbols_repo.set_universe(["AAPL"])
    src = Source(payload, ny(2026, 11, 21, 12, 0))
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: src)
    chains._fill_attempts.clear()
    hooks.chain_preparers.append(ingest.complete_chain)
    yield src
    hooks.chain_preparers.clear()


async def test_after_hours_fill_once_never_archived(server, monkeypatch):
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 11, 20, 11, 0))
    assert (await chains.sweep_fill()).startswith("skipped")                   # session open
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 11, 21, 12, 0))     # Saturday
    detail = await chains.sweep_fill()
    assert detail.startswith("fill of 2026-11-20: questrade: 1 chains") and ("AAPL", False) in server.calls
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["after_hours"] is True and m["source"] == "questrade" and eod_source() is None
    rows = options_repo.latest_source_iv("AAPL", ny(2026, 11, 20, 9, 30))
    assert rows and {r["iv_source"] for r in rows.values()} == {"questrade"}   # preparers labelled the source
    n = len(server.calls)
    assert await chains.sweep_fill() == "questrade: session 2026-11-20 covered" and len(server.calls) == n
    assert server.kept == 2                                                    # idle credentials kept alive


async def test_final_sweep_window(server, monkeypatch):
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 11, 20, 20, 30))
    assert (await chains.sweep_final()).startswith("skipped")
    server.when = ny(2026, 11, 20, 16, 21)
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 11, 20, 16, 21))
    assert (await chains.sweep_final()).startswith("questrade: 1 chains")
    assert ("AAPL", True) in server.calls and eod_source() == "questrade"
    a = options_repo.eod_quality(FRI)["AAPL"]
    assert (a["source"], a["delayed"]) == ("questrade", False) and a["quoted"] > 0
    assert date(2026, 11, 20) in options_repo.eod_dates()
