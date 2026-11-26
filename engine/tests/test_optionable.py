"""Listed options per chain source (jobs/optionable.py, docs/INTEGRATION.md §5 "Listed options"): one source's
"none" is that source's answer; the symbol is unoptionable only once every chain source serving it answered so; a
chain from any source makes it optionable; the bridge's chain universe and the server sweeps skip only symbols the
source itself answered "none" for; the migration of single-source false rows."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone

import duckdb
import pytest

from optionlens import db
from optionlens.jobs import bridge_plan, chains, optionable, source_health
from optionlens.repo import symbols as symbols_repo
from optionlens.repo import user as user_repo
from optionlens.sources import chain_source

from .test_chain_sources import payload, snap, tmpdb  # noqa: F401  (fixtures)
from .test_refresh import Src, bridge_connected


class EmptySrc(Src):
    """A server-side source that lists no options for any symbol (an empty chain)."""

    async def fetch_chain(self, symbol, final=False):
        self.calls.append(symbol)
        return dataclasses.replace(snap(self.payload, datetime.now(timezone.utc)), contracts=())


@pytest.fixture
def qt(tmpdb, payload, monkeypatch):
    s = EmptySrc(payload)
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: s)
    source_health.reset("questrade")
    yield s
    source_health.reset("questrade")


async def bridge_no_options(sym: str = "AAPL") -> bool | None:
    """The bridge's "no options" (what POST /api/ingest/no-options records; the API: tests/api/test_no_chain.py)."""
    return await asyncio.to_thread(optionable.confirm_none, sym, "ibkr", "no_options")


def flag(sym: str = "AAPL"):
    return symbols_repo.get_symbol(sym)["optionable"]


async def test_one_source_none_is_not_the_symbols_answer_while_the_bridge_has_not_answered(qt):
    bridge_connected()                                     # the bridge is set up: it serves US listings too
    assert await chains.refresh_chain(qt, "AAPL") is None
    assert flag() is None and symbols_repo.verdicts("AAPL") == {"questrade": "no_options"}
    user_repo.create_watchlist("w", ["AAPL"])
    assert "AAPL" in bridge_plan.chain_universe()          # Questrade's answer does not keep the bridge from asking
    assert bridge_plan.for_bridge(["AAPL"]) == ["AAPL"]    # ... and a healthy Questrade does not cover it
    assert await bridge_no_options() is False
    assert flag() is False                                 # every serving source answered: no listed options
    assert "AAPL" not in bridge_plan.chain_universe()      # the bridge's own answer keeps it out


async def test_the_only_set_up_source_decides(qt):
    assert await chains.refresh_chain(qt, "AAPL") is None  # no bridge ever reported: Questrade alone serves AAPL
    assert flag() is False


async def test_a_chain_from_either_source_makes_the_symbol_optionable(qt, payload):
    bridge_connected()
    await bridge_no_options()
    assert flag() is None                                  # Questrade (connected) has not answered
    chains.store_chain(snap(payload, datetime.now(timezone.utc)), datetime.now(timezone.utc), source="questrade")
    assert flag() is True and symbols_repo.verdicts("AAPL") == {"ibkr": "no_options"}
    await chains.refresh_chain(qt, "AAPL")                 # Questrade lists none after all
    assert flag() is False
    chains.store_chain(snap(payload, datetime.now(timezone.utc)), datetime.now(timezone.utc), source="ibkr")
    assert flag() is True and symbols_repo.verdicts("AAPL") == {"questrade": "no_options"}


async def test_not_listed_counts_as_that_sources_none(qt):
    bridge_connected()
    optionable.confirm_none("AAPL", "questrade", "not_listed")
    assert flag() is None                                  # nobody said "no options" yet
    assert optionable.confirm_none("AAPL", "ibkr", "no_options") is False


async def test_sweeps_skip_only_symbols_the_source_itself_answered_none_for(qt, monkeypatch):
    symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"} for s in ("MSFT", "TSLA")])
    symbols_repo.set_universe(["AAPL", "MSFT", "TSLA"])
    monkeypatch.setattr(bridge_plan, "EOD_INDICES", ())
    symbols_repo.record_verdict("MSFT", "questrade", "no_options")
    symbols_repo.record_verdict("TSLA", "ibkr", "no_options")
    bridge_connected()
    monkeypatch.setattr(chains, "now_ny", lambda: datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc))
    detail = await chains.sweep()
    assert sorted(qt.calls) == ["AAPL", "TSLA"] and "1 it answered none for not asked" in detail
    qt.calls.clear()
    await chains.sweep(["MSFT"])                           # an explicit list is asked whatever the answers
    assert qt.calls == ["MSFT"]


def test_migration_unmarks_false_rows_set_by_a_single_source(tmp_path):
    db.close()
    p = tmp_path / "old.duckdb"
    con = duckdb.connect(str(p))
    con.execute("""CREATE TABLE symbols (symbol VARCHAR PRIMARY KEY, name VARCHAR, kind VARCHAR NOT NULL,
                   sector VARCHAR, industry VARCHAR, market_cap DOUBLE, in_universe BOOLEAN NOT NULL DEFAULT false,
                   optionable BOOLEAN, updated_at TIMESTAMP)""")
    con.execute("""INSERT INTO symbols (symbol, kind, optionable) VALUES ('XSP', 'index', false), ('AAPL', 'stock', true),
                   ('ZZZ', 'stock', NULL)""")
    con.close()
    db.connect(p)
    try:
        assert [flag(s) for s in ("XSP", "AAPL", "ZZZ")] == [None, True, None]   # false from one source: unknown
        symbols_repo.record_verdict("XSP", "ibkr", "no_options")
        symbols_repo.set_optionable("XSP", False)
        db.close()
        db.connect(p)                                                            # idempotent: a confirmed false stays
        assert flag("XSP") is False
    finally:
        db.close()
