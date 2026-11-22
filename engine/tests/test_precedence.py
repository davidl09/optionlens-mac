"""Chain precedence between sources (jobs/precedence.py) through the store pipeline, IV borrowed between sources,
the bridge's closing-chain want, and the server-side final / after-hours-fill sweeps (offline)."""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta

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

T0 = ny(2026, 11, 20, 16, 25)


def q(source, final=True, realtime=True, quoted=100, at=T0, session=FRI):
    return Quality(source, final, realtime, quoted, at, session)


# ----------------------------------------------------------------------------- the rule
def test_rule_order():
    later = T0 + timedelta(minutes=5)
    assert precedence.replaces(None, q("questrade"))
    assert precedence.replaces(q("ibkr", final=False, quoted=900), q("questrade", realtime=False, quoted=1))  # final
    assert not precedence.replaces(q("ibkr", quoted=1), q("questrade", realtime=False, quoted=900))         # real-time
    assert precedence.replaces(q("ibkr", realtime=False), q("questrade", realtime=True))
    assert precedence.replaces(q("ibkr", quoted=100), q("questrade", quoted=101))                            # coverage
    assert precedence.replaces(q("ibkr"), q("questrade", at=later))                                          # fresher
    assert precedence.replaces(q("questrade"), q("ibkr", at=later))                                          # no source
    assert not precedence.replaces(q("questrade", at=later), q("ibkr"))                                      # preferred
    assert not precedence.replaces(q("ibkr", at=later), q("ibkr", realtime=False))     # same source: same rule
    assert precedence.replaces(q("ibkr", quoted=900), q("ibkr", quoted=900))           # the same chain re-stored
    assert precedence.replaces(q("ibkr", quoted=1), q("vendor", quoted=1, at=later))   # unlisted sources: same rule


def test_intraday_snapshots_far_apart_and_sessions():
    t = ny(2026, 11, 20, 10, 0)
    old_rt = q("questrade", final=False, at=t)
    assert precedence.replaces(old_rt, q("ibkr", final=False, realtime=False, at=t + timedelta(minutes=16)))
    assert not precedence.replaces(old_rt, q("ibkr", final=False, realtime=False, at=t + timedelta(minutes=14)))
    assert not precedence.replaces(q("questrade"), q("ibkr", final=False, at=ny(2026, 11, 20, 21, 0)))  # fill < final
    monday = q("ibkr", final=False, realtime=False, quoted=1, at=ny(2026, 11, 23, 9, 31), session=date(2026, 11, 23))
    assert precedence.replaces(q("questrade"), monday) and not precedence.replaces(monday, q("questrade"))
    assert precedence.replaces(Quality("cboe", False, False, 0), q("ibkr", final=False))   # legacy rows: oldest


def test_quality_of_snapshot_and_rows(payload):
    s = dataclasses.replace(snap(payload, T0), delayed=False)
    qs = precedence.of_snapshot(s, "questrade", True)
    assert qs.realtime and qs.final and 0 < qs.quoted <= len(s.contracts)
    assert qs.quoted == sum(1 for c in s.contracts if c.ask and c.ask > 0 and options_repo.archivable(c))
    assert qs.fetched_at == T0 and qs.session == FRI
    assert precedence.of_metrics_row({"source": "cboe"}) == Quality("cboe", False, False, 0)   # legacy row: unknown
    assert not precedence.of_snapshot(dataclasses.replace(s, delayed=None), "x", False).realtime


# ----------------------------------------------------------------------------- through store_chain
def _thin(s: ChainSnapshot, keep: float) -> ChainSnapshot:
    return dataclasses.replace(s, contracts=s.contracts[: int(len(s.contracts) * keep)])


def test_real_time_final_beats_delayed_ibkr_everywhere(tmpdb, payload):
    t_ib, t_qt = ny(2026, 11, 20, 16, 25), ny(2026, 11, 20, 16, 21)
    chains.store_chain(dataclasses.replace(snap(payload, t_ib), delayed=True), t_ib, final=True, source="ibkr")
    assert eod_source() == "ibkr" and options_repo.latest_source("AAPL") == "ibkr"
    chains.store_chain(dataclasses.replace(snap(payload, t_qt, price=350.0), delayed=False), t_qt, final=True,
                       source="questrade")
    assert eod_source() == "questrade" and options_repo.eod_quality(FRI)["AAPL"]["delayed"] is False
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["source"] == "questrade" and m["chain_final"] is True and m["chain_quoted"] > 0 and m["close"] == 350.0
    assert options_repo.latest_source("AAPL") == "questrade"                      # served: the better chain
    from optionlens.services import symbol_options
    assert symbol_options._source("AAPL") == {"source": "questrade"}             # Chain.source
    later = ny(2026, 11, 20, 16, 40)
    chains.store_chain(dataclasses.replace(snap(payload, later), delayed=True), later, final=True, source="ibkr")
    assert eod_source() == "questrade" and metrics_repo.source_of(FRI, "AAPL") == "questrade"
    assert options_repo.latest_source("AAPL") == "questrade"


def test_more_quoted_contracts_win_then_the_fresher_fetch(tmpdb, payload):
    t = ny(2026, 11, 20, 16, 25)
    full = dataclasses.replace(snap(payload, t), delayed=False)
    chains.store_chain(_thin(full, 0.5), t, final=True, source="ibkr")
    chains.store_chain(full, t, final=True, source="questrade")                 # more complete
    assert eod_source() == "questrade" and options_repo.latest_source("AAPL") == "questrade"
    t2 = ny(2026, 11, 20, 16, 30)
    chains.store_chain(dataclasses.replace(full, fetched_at=t2), t2, final=True, source="ibkr")   # as complete, fresher
    assert eod_source() == "ibkr" and metrics_repo.source_of(FRI, "AAPL") == "ibkr"
    assert options_repo.latest_source("AAPL") == "ibkr"
    chains.store_chain(full, t, final=True, source="questrade")                 # as complete, older: kept out
    assert eod_source() == "ibkr" and options_repo.latest_source("AAPL") == "ibkr"


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


def test_a_losing_chain_lends_its_source_ivs_to_the_served_chain(tmpdb, payload):
    """IBKR's real-time chain wins but lacks IV on some contracts (computed); Questrade's later, delayed chain loses
    yet has a source IV for them: the served chain, its metrics row and archive take it (any source's IV before a
    computed one)."""
    t1, t2 = ny(2026, 11, 20, 16, 21), ny(2026, 11, 20, 16, 23)
    base = snap(payload, t1)
    bare = dataclasses.replace(base, delayed=False, contracts=tuple(
        dataclasses.replace(c, iv=None, delta=None, gamma=None, theta=None, vega=None) for c in base.contracts))
    ib = ingest.complete_chain(bare, "ibkr")
    chains.store_chain(ib, t1, final=True, source="ibkr")
    computed = {c.osi for c in ib.contracts if c.iv_source and c.iv_source.startswith("computed")}
    assert computed and metrics_repo.get_metrics("AAPL", FRI)["iv_computed"] is True
    qt = ingest.complete_chain(dataclasses.replace(snap(payload, t2), delayed=True), "questrade")
    given = {c.osi: c for c in qt.contracts if c.iv_source == "questrade"}
    assert not chains.store_chain(qt, t2, final=True, source="questrade")       # loses: delayed
    assert options_repo.latest_source("AAPL") == "ibkr" and eod_source() == "ibkr"
    rows = options_repo.latest_source_iv("AAPL", ny(2026, 11, 20, 9, 30))
    osi = next(o for o in computed if o in given)
    assert rows[osi]["iv_source"] == "questrade" and rows[osi]["iv"] == pytest.approx(given[osi].iv)
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["source"] == "ibkr" and m["iv_computed"] is False                    # rebuilt from source IVs
    eod = options_repo.eod_for_date(FRI, ["AAPL"])
    assert "questrade" in set(eod["iv_source"])


# ----------------------------------------------------------------------------- server-side sweeps
class Source:
    """A connected server-side source serving the recorded AAPL chain as real-time data."""
    name = "questrade"

    def __init__(self, payload, when):
        self.payload, self.when, self.calls, self.kept = payload, when, [], 0
        self.fail: Exception | None = None     # raised by fetch_chain while set
        self.why: str | None = None            # unavailable() while set

    def connected(self):
        return True

    def unavailable(self):
        return self.why

    def problem(self):
        return None

    async def keepalive(self):
        self.kept += 1

    def status(self):
        return {}

    async def fetch_chain(self, symbol, final=False):
        self.calls.append((symbol, final))
        if self.fail is not None:
            raise self.fail
        if symbol != "AAPL":
            raise chain_source.NotListed(symbol)
        return dataclasses.replace(snap(self.payload, self.when), delayed=False)


@pytest.fixture
def server(tmpdb, payload, monkeypatch):
    symbols_repo.set_universe(["AAPL"])
    src = Source(payload, ny(2026, 11, 21, 12, 0))
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: src)
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


async def test_failed_fill_attempts_are_not_coverage(server, monkeypatch):
    """A fill whose fetches all failed (e.g. 403 code 1016) leaves the session wanted: asked again after the backoff,
    not reported as covered; and not at all while the source needs attention."""
    from optionlens.net import FetchError
    sat = ny(2026, 11, 21, 12, 0)
    monkeypatch.setattr(chains, "now_ny", lambda: sat)
    server.fail = FetchError("u", 503)
    detail = await chains.sweep_fill()
    assert "5 failed" in detail and "(failures: 5 × 503 FetchError)" in detail   # AAPL + the 4 indices
    n = len(server.calls)
    server.fail = None
    detail = await chains.sweep_fill()
    assert "failed recently" in detail and "covered" not in detail and len(server.calls) == n   # backoff
    later = sat + timedelta(seconds=chains.FAILED_RETRY_S + 60)
    monkeypatch.setattr(chains, "now_ny", lambda: later)
    server.why = "needs attention"
    assert await chains.sweep_fill() == "questrade: needs attention" and len(server.calls) == n
    server.why = None
    assert (await chains.sweep_fill()).startswith("fill of 2026-11-20: questrade: 1 chains")
    assert await chains.sweep_fill() == "questrade: session 2026-11-20 covered"


def test_wants_split_chain_work_with_a_healthy_server_source(server):
    """While Questrade is healthy it sweeps the universe: the bridge is asked only for the symbols it failed or does
    not list. Unavailable or failing: the bridge gets its full role back."""
    from optionlens.jobs import source_health
    symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"} for s in ("MSFT", "NVDA", "TSLA")])
    symbols_repo.set_universe(["AAPL", "MSFT", "NVDA", "TSLA"])
    from optionlens.repo import user as user_repo
    wl = user_repo.create_watchlist("w", ["AAPL", "MSFT", "NVDA", "TSLA"])
    assert wl
    live = ny(2026, 11, 20, 11, 0)
    full = bridge_plan.chain_universe(live)
    assert set(full) == {"AAPL", "MSFT", "NVDA", "TSLA"}
    assert bridge_plan.chain_universe_want(live)["symbols"] == []                 # covered: not swept yet, assumed
    source_health.record("questrade", "MSFT", "failed", "503 code 1011 (Questrade timed out)")
    source_health.record("questrade", "NVDA", "not_listed")
    source_health.record("questrade", "AAPL", "ok")
    assert bridge_plan.chain_universe_want(live)["symbols"] == ["MSFT", "NVDA"]
    assert bridge_plan.eod_chains_want(ny(2026, 11, 20, 17, 0))["symbols"] == ["MSFT", "NVDA"]   # indices swept too
    server.why = "needs attention"
    assert bridge_plan.chain_universe_want(live)["symbols"] == full                # unavailable: full role
    server.why = None
    for i in range(20):
        source_health.record("questrade", f"X{i}", "failed", "503")
    assert source_health.failing("questrade")
    assert bridge_plan.chain_universe_want(live)["symbols"] == full                # failing: full role
