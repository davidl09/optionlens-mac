"""The IBKR bridge for Canadian listings (docs/INTEGRATION.md §6 "Markets"): the wants plan each market on its own
calendar, pushed Montréal Exchange chains are stored under the codes Questrade's are (so the two sources merge and
chain precedence decides), IV / HV history, option bars and stats of a Canadian symbol are stored in its market's
session and currency, and a symbol lists no options only once every source serving it said so."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from optionlens import db
from optionlens.analytics import markets
from optionlens.analytics.calendar import NY
from optionlens.api import routes_ingest
from optionlens.jobs import bridge_plan, chains, optionable
from optionlens.models import Bar
from optionlens.repo import bars as bars_repo
from optionlens.repo import market as market_repo
from optionlens.repo import metrics as metrics_repo
from optionlens.repo import options as options_repo
from optionlens.repo import quotes as quotes_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.repo import user as user_repo
from optionlens.repo import vol_history as vol_history_repo
from optionlens.services import context, ingest
from optionlens.services.errors import BadRequest
from optionlens.sources import chain_source
from optionlens.sources.questrade import mapping

from .test_refresh import bridge_connected

MX = json.loads((Path(__file__).parent / "fixtures" / "ibkr_mx_chain.json").read_text())
SYM = MX["symbol"]
WED, THANKSGIVING, FRI = date(2026, 11, 25), date(2026, 11, 26), date(2026, 11, 27)   # US Thanksgiving: the TSX trades
VICTORIA_DAY = date(2027, 5, 24)                                                       # the TSX is closed, NYSE trades


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    context.invalidate()
    symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"} for s in ("AAPL", SYM)])
    symbols_repo.set_universe(["AAPL"])
    symbols_repo.set_universe([SYM], markets.CA)
    for s, px in (("AAPL", 300.0), (SYM, 121.0)):          # a price history: ingest fetches none
        bars_repo.upsert_bars([Bar(s, date(2026, 9, 1) + timedelta(days=i), px, px, px, px, 1000) for i in range(87)])
    yield
    db.close()
    context.invalidate()


def plans(want: list[dict]) -> dict[str, dict]:
    return {p["market"]: p for p in want}


def push(fetched: datetime, final: bool = False, session: date | None = None, **quote) -> dict:
    """A chain body as the bridge posts it for the fixture's contracts (validated like the API does)."""
    q = {"bid": 2.0, "ask": 2.2, "last": 2.1, "volume": 10, "oi": 100, **quote}
    body = {"source": "ibkr", "symbol": SYM, "fetched_at": fetched.isoformat(), "final": final, "delayed": True,
            "session_date": session.isoformat() if session else None,
            "underlying": {"price": 121.0, "prev_close": 120.0, "ts": fetched.isoformat()},
            "contracts": [{**c, **q} for c in MX["posted"]]}
    return routes_ingest.ChainIn(**body).model_dump()


def questrade_snapshot(fetched: datetime, delayed: bool = False):
    """The same contracts as Questrade's MX chain maps them (sources/questrade/mapping.py)."""
    by_expiry: dict[str, list[dict]] = {}
    ids = {}
    for i, c in enumerate(MX["posted"]):
        row = {"strikePrice": c["strike"], "callSymbolId": None, "putSymbolId": None}
        row["callSymbolId" if c["right"] == "C" else "putSymbolId"] = ids[i] = 9000 + i
        by_expiry.setdefault(c["expiry"], []).append(row)
    payload = {"optionChain": [{"expiryDate": f"{e}T00:00:00.000000-05:00", "listingExchange": "MX",
                                "chainPerRoot": [{"optionRoot": "RY", "multiplier": 100, "chainPerStrikePrice": rows}]}
                               for e, rows in by_expiry.items()]}
    _, listed = mapping.listed_contracts(payload, SYM)
    contracts = tuple(mapping.contract(SYM, listed[i], {"symbolId": i, "bidPrice": 2.0, "askPrice": 2.2,
                                                         "lastTradePrice": 2.1, "volume": 10, "openInterest": 100,
                                                         "volatility": 22.0, "delay": 0}) for i in ids.values())
    snap = ingest.complete_chain(dataclasses.replace(
        ingest.ChainSnapshot(ingest._quote(SYM, {"price": 121.0, "prev_close": 120.0}, fetched, delayed, False),
                             contracts, fetched, delayed, None)), "questrade")
    return snap


def stored_codes() -> list[str]:
    return sorted(c.osi for c in options_repo.get_chain(SYM).contracts)


# ----------------------------------------------------------------------------- wants
def test_the_wants_plan_each_market_on_its_own_calendar(tmpdb):
    user_repo.create_watchlist("w", ["AAPL", SYM])
    midday = ny(2026, 11, 26, 11, 0)                        # US Thanksgiving: only Toronto trades
    w = ingest.wants(midday)
    assert w["version"] == 2
    us, ca = plans(w["underlying_stats"])["US"], plans(w["underlying_stats"])["CA"]
    assert ca == {"market": "CA", "session_date": THANKSGIVING, "symbols": [SYM], "max_age_s": bridge_plan.STATS_MAX_AGE_S}
    assert us["session_date"] == us["as_of_session"] == WED and us["symbols"] == ["AAPL"]   # a fill of Wednesday
    cu = plans(w["chain_universe"])
    assert cu["CA"]["symbols"] == [SYM] and "as_of_session" not in cu["CA"]
    assert cu["US"]["symbols"] == ["AAPL"] and cu["US"]["as_of_session"] == WED
    eod = plans(ingest.wants(ny(2026, 11, 26, 16, 30))["eod_chains"])
    assert eod["CA"]["session_date"] == THANKSGIVING and eod["CA"]["symbols"] == [SYM]
    assert eod["CA"]["not_before"] == ny(2026, 11, 26, 16, 20) and eod["CA"]["deadline"] == ny(2026, 11, 26, 20, 0)
    assert eod["US"]["session_date"] == WED and SYM not in eod["US"]["symbols"] and "SPX" in eod["US"]["symbols"]
    # the day after: NYSE closes at 13:00, Toronto at 16:00
    early = plans(ingest.wants(ny(2026, 11, 27, 14, 0))["eod_chains"])
    assert early["US"]["not_before"] == ny(2026, 11, 27, 13, 20) and early["CA"]["not_before"] == ny(2026, 11, 27, 16, 20)
    # Victoria Day: the other way round
    v = ingest.wants(ny(2027, 5, 24, 11, 0))
    assert "as_of_session" not in plans(v["underlying_stats"])["US"]
    assert plans(v["underlying_stats"])["CA"]["as_of_session"] == date(2027, 5, 21)
    assert plans(v["chain_universe"])["CA"]["as_of_session"] == date(2027, 5, 21)


def test_history_dividend_and_option_bar_wants_cover_canadian_symbols(tmpdb):
    user_repo.create_watchlist("w", ["AAPL", SYM])
    evening = ny(2026, 11, 26, 21, 0)                       # Thanksgiving evening: Toronto's session is complete
    w = ingest.wants(evening)
    for kind in ("iv_history", "hv_history"):
        assert {x["symbol"] for x in w[kind]} >= {"AAPL", SYM}
    assert w["history_symbols"] == len({x["symbol"] for x in w["iv_history"]})
    assert {x["symbol"] for x in w["dividend_history"]} == {"AAPL", SYM}
    # a backfilled series is current up to its own market's last completed session
    full = [(date(2026, 1, 1) + timedelta(days=i), 0.2) for i in range(329)]                 # ... 2026-11-25
    for s in ("AAPL", SYM):
        vol_history_repo.upsert(s, "iv30", "ibkr", full)
    iv = {x["symbol"]: x["from"] for x in ingest.wants(evening)["iv_history"]}
    assert "AAPL" not in iv and iv[SYM] == THANKSGIVING     # NYSE had no session; the TSX one is missing
    # option-bar catch-up: each symbol's sessions are its market's
    for s in ("AAPL", SYM):
        bars_repo.upsert_bars([Bar(s, d, None, None, None, 10.0, 1) for d in (WED, THANKSGIVING)])
    bars = {x["symbol"]: x["sessions"] for x in ingest.wants(evening)["option_bars"]}
    assert THANKSGIVING in bars[SYM] and THANKSGIVING not in bars["AAPL"] and WED in bars["AAPL"]


def test_live_wants_include_canadian_symbols_on_screen():
    from optionlens.live.hub import LiveHub
    from optionlens.live.subscriptions import Wanted
    h = LiveHub.__new__(LiveHub)
    h.active, h.sessions = "ibkr", {"US": "regular", "CA": "regular"}
    h.registry = type("R", (), {"union": lambda self: Wanted(frozenset({"AAPL", SYM}), {})})()
    assert h.bridge_wants()["symbols"] == ["AAPL", SYM]


# ----------------------------------------------------------------------------- chains
def test_a_pushed_mx_chain_is_stored_under_questrades_codes(tmpdb):
    t = ny(2026, 11, 26, 11, 0)
    assert ingest.chain(push(t), now=t) == {"stored": 4, "symbol": SYM}
    assert stored_codes() == sorted(MX["osi"])
    assert sorted(c.osi for c in questrade_snapshot(t).contracts) == sorted(MX["osi"])   # the two sources merge
    assert options_repo.latest_source(SYM) == "ibkr" and symbols_repo.get_symbol(SYM)["optionable"] is True
    assert options_repo.get_chain("RY") is None                                          # never the NYSE listing's
    assert metrics_repo.symbols_on(THANKSGIVING) == {SYM}                                # Toronto's session, US closed
    q = quotes_repo.get_quote(SYM)
    assert q.price == 121.0 and markets.currency_of(q.symbol) == "CAD"


def test_a_closing_mx_chain_belongs_to_torontos_session(tmpdb):
    t = ny(2026, 11, 26, 16, 25)                            # NYSE is closed all day: its "session" would be Wednesday
    ingest.chain(push(t, final=True, session=THANKSGIVING), now=t)
    assert options_repo.eod_sources([THANKSGIVING], [SYM])[THANKSGIVING] == {SYM: "ibkr"}
    with pytest.raises(BadRequest):                         # before Toronto's close
        ingest.chain(push(ny(2026, 11, 26, 15, 0), final=True, session=THANKSGIVING), now=t)
    with pytest.raises(BadRequest):                         # Victoria Day is not a Toronto session
        late = ny(2027, 5, 24, 16, 25)
        ingest.chain(push(late, final=True, session=VICTORIA_DAY), now=late)


def test_chain_precedence_between_questrade_and_ibkr_for_a_canadian_symbol(tmpdb):
    t_ib, t_qt = ny(2026, 11, 26, 16, 25), ny(2026, 11, 26, 16, 21)
    ingest.chain(push(t_ib, final=True, session=THANKSGIVING), now=t_ib)                 # IBKR: delayed
    assert options_repo.latest_source(SYM) == "ibkr"
    chains.store_chain(questrade_snapshot(t_qt), t_qt, final=True, source="questrade")   # real-time beats delayed
    assert options_repo.latest_source(SYM) == "questrade"
    assert options_repo.eod_sources([THANKSGIVING], [SYM])[THANKSGIVING] == {SYM: "questrade"}
    assert stored_codes() == sorted(MX["osi"])                                           # one chain, the same keys
    ingest.chain(push(t_ib + timedelta(minutes=5), final=True, session=THANKSGIVING), now=t_ib + timedelta(minutes=5))
    assert options_repo.latest_source(SYM) == "questrade"                                # still: fresher but delayed
    assert "RY.TO" not in bridge_plan.eod_chains_want(ny(2026, 11, 26, 17, 0), markets.CA)["symbols"]   # settled


def test_a_canadian_symbol_lists_no_options_only_when_every_source_says_so(tmpdb, monkeypatch):
    class Qt:
        name = "questrade"
        def connected(self): return True
        def unavailable(self): return None
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: Qt())
    monkeypatch.setattr(optionable, "get_settings", lambda: type("S", (), {"chain_sources": ("ibkr-bridge", "questrade")})())
    monkeypatch.setattr(chain_source, "get_settings", optionable.get_settings)
    bridge_connected()                                      # the bridge is set up: it serves Canadian listings too
    assert optionable.serving(SYM) == {"ibkr", "questrade"}
    assert optionable.confirm_none(SYM, "questrade", "no_options") is None               # one source's answer
    assert ingest.no_options({"source": "ibkr", "symbol": SYM}) == {"symbol": SYM, "optionable": False}
    t = ny(2026, 11, 26, 11, 0)
    ingest.chain(push(t), now=t)                            # a chain from either source: optionable again
    assert symbols_repo.get_symbol(SYM)["optionable"] is True and symbols_repo.verdicts(SYM) == {"questrade": "no_options"}


# ----------------------------------------------------------------------------- series, bars, stats
def test_iv_and_hv_history_of_a_canadian_symbol_feed_its_iv_rank(tmpdb):
    days = [date(2026, 1, 2) + timedelta(days=i) for i in range(329)]                    # ... 2026-11-26
    body = {"source": "ibkr", "symbol": SYM, "bars": [{"date": d, "value": 0.15 + 0.1 * i / 328} for i, d in enumerate(days)]}
    assert ingest.vol_history("iv30", {**body, "kind": "iv30"}) == {"stored": 329}
    assert ingest.vol_history("hv30", {**body, "kind": "hv30"}) == {"stored": 329}
    info = context.iv_rank_info(SYM)
    assert info["iv30"] == pytest.approx(0.25) and info["iv30_asof"] == THANKSGIVING and info["history_days"] > 200
    assert context.iv_rank_info("RY")["iv30"] is None       # the NYSE listing has its own series
    assert set(vol_history_repo.last_dates("hv30", [SYM])) == {SYM}


def test_option_bars_of_a_canadian_symbol_use_its_codes_session_and_rate(tmpdb):
    market_repo.upsert_rates([(WED, 0.09)])                 # USD: must not be used
    market_repo.upsert_rates([(WED, 0.026)], "CAD")
    bars_repo.upsert_bars([Bar(SYM, WED, 121.0, 121.0, 121.0, 121.0, 1000)])
    rows = [{**c, "expiry": date.fromisoformat(c["expiry"]), "date": WED, "close": 3.0, "volume": 5} for c in MX["posted"]]
    assert ingest.option_bars({"source": "ibkr", "symbol": SYM, "bars": rows}) == {"stored": 4}
    eod = options_repo.eod_for_date(WED, [SYM])
    assert sorted(eod["osi"].tolist()) == sorted(MX["osi"]) and set(eod["underlying"].tolist()) == {121.0}
    assert options_repo.eod_sources([WED], [SYM])[WED] == {SYM: options_repo.RECONSTRUCTED}
    from optionlens.jobs import catchup
    spot_rows = catchup.reconstruct_rows(SYM, WED, rows[:1], 121.0, 0.026, 0.0)
    assert spot_rows[0]["osi"] == MX["osi"][0] and spot_rows[0]["iv"] > 0


def test_stats_are_stored_in_the_session_of_the_symbols_market(tmpdb):
    t = ny(2026, 11, 26, 11, 0)
    body = {"source": "ibkr", "delayed": True, "session_date": THANKSGIVING, "ts": t,
            "stats": [{"symbol": SYM, "price": 121.0, "call_volume": 100, "put_volume": 50, "iv30": 0.2}]}
    assert ingest.underlying_stats(body, now=t) == {"stored": 1}
    assert metrics_repo.symbols_on(THANKSGIVING) == {SYM}
    us = {**body, "stats": [{"symbol": "AAPL", "price": 300.0}]}
    with pytest.raises(BadRequest):                         # NYSE is closed: these are Wednesday's frozen values
        ingest.underlying_stats(us, now=t)
    assert ingest.underlying_stats({**us, "session_date": WED}, now=t) == {"stored": 1}
    with pytest.raises(BadRequest, match="one market"):
        ingest.underlying_stats({**body, "stats": [*body["stats"], *us["stats"]]}, now=t)
