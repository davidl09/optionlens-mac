"""After-hours fills (docs/INTEGRATION.md §6): what the bridge is asked for when the current session is uncovered
(weekend, evening, weekday morning before the open), and the precedence of such frozen-data pushes against the
session's own data (never overwritten) and its closing snapshot (always wins)."""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from optionlens import db
from optionlens.analytics.calendar import NY
from optionlens.jobs import bridge_plan, chains
from optionlens.models import Bar
from optionlens.repo import bars as bars_repo
from optionlens.repo import metrics as metrics_repo
from optionlens.repo import options as options_repo
from optionlens.repo import quotes as quotes_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.repo import user as user_repo
from optionlens.repo import views as views_repo
from optionlens.repo import vol_history as vol_history_repo
from optionlens.services.errors import BadRequest
from optionlens.sources import cboe

FIX = Path(__file__).parent / "fixtures"
THU, FRI, MON = date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)
UNIVERSE = ["AAPL", "MSFT", "TSLA", "NVDA", "F"]


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"} for s in UNIVERSE])
    symbols_repo.set_universe(UNIVERSE)
    yield
    db.close()


@pytest.fixture(scope="module")
def payload():
    return json.loads((FIX / "cboe_chain_AAPL.json").read_text())


def snap(payload, fetched: datetime, price: float | None = None):
    s = cboe.parse_chain(payload, "AAPL", fetched_at=fetched)
    return s if price is None else dataclasses.replace(s, quote=dataclasses.replace(s.quote, price=price))


def stat(sym: str = "AAPL", **kw) -> dict:
    return {"symbol": sym, "price": 351.0, "change_pct": 0.004, "volume": 4.0e7, "call_volume": 300000,
            "put_volume": 200000, "call_oi": None, "put_oi": None, "iv30": None, "hv30": None, **kw}


# ----------------------------------------------------------------------------- wants
@pytest.mark.parametrize("now,session", [
    (ny(2026, 11, 21, 12, 0), FRI),       # Saturday
    (ny(2026, 11, 20, 21, 0), FRI),       # Friday evening, after the 20:00 ET window
    (ny(2026, 11, 23, 8, 0), FRI),        # Monday morning before the open (and before the chain window)
    (ny(2026, 11, 20, 8, 0), THU),        # a weekday morning: the previous session
])
def test_uncovered_session_wants_stats_and_chains_outside_the_live_hours(tmpdb, monkeypatch, now, session):
    user_repo.create_watchlist("w", ["F"])
    views_repo.record_view("TSLA", now - timedelta(days=1))
    for sym, v in (("NVDA", 900), ("AAPL", 500), ("MSFT", 100)):       # an older session: liquidity ranking
        metrics_repo.upsert_metrics(session - timedelta(days=7), sym, {"call_volume": v, "put_volume": v})
    monkeypatch.setattr(bridge_plan, "get_settings", lambda: type("S", (), {"chain_universe_size": 2})())
    us = bridge_plan.underlying_stats_want(now)
    assert us == {"market": "US", "session_date": session, "symbols": ["F", "TSLA", "NVDA", "AAPL", "MSFT"],
                  "max_age_s": bridge_plan.FILL_MAX_AGE_S, "as_of_session": session}                            # watchlist, recently opened, most liquid
    cu = bridge_plan.chain_universe_want(now)
    assert cu["symbols"] == ["F", "TSLA", "NVDA", "AAPL"] and cu["as_of_session"] == session
    assert cu["max_age_s"] == bridge_plan.FILL_MAX_AGE_S


def test_live_hours_want_the_session_itself(tmpdb):
    assert "as_of_session" not in bridge_plan.underlying_stats_want(ny(2026, 11, 20, 11, 0))
    assert "as_of_session" not in bridge_plan.chain_universe_want(ny(2026, 11, 23, 9, 5))   # pre-market chains
    assert bridge_plan.underlying_stats_want(ny(2026, 11, 23, 9, 5))["as_of_session"] == FRI  # stats: from the open


def test_stats_fill_only_while_the_session_is_uncovered(tmpdb):
    sat = ny(2026, 11, 21, 12, 0)
    chains.store_underlying_stats(FRI, [stat(s) for s in ("AAPL", "MSFT", "TSLA", "NVDA")], "ibkr", after_hours=True)
    assert bridge_plan.underlying_stats_want(sat)["symbols"] == []    # 1 of 5 missing = 20%: covered
    metrics_repo.upsert_metrics(THU, "F", {"call_volume": 1})          # another session's row does not count
    chains.store_underlying_stats(FRI, [stat("TSLA")], "ibkr", after_hours=True)
    with db.write() as cur:
        cur.execute("DELETE FROM metrics_daily WHERE date = ? AND symbol IN ('AAPL', 'MSFT')", [FRI])
    # 60% missing: the missing ones, F first (it has stored option volume: most liquid before the unknown)
    assert bridge_plan.underlying_stats_want(sat)["symbols"] == ["F", "AAPL", "MSFT"]


def test_chain_fill_skips_symbols_with_a_chain_of_the_session(tmpdb, payload):
    user_repo.create_watchlist("w", ["AAPL", "MSFT"])
    sat = ny(2026, 11, 21, 12, 0)
    chains.store_chain(snap(payload, ny(2026, 11, 19, 15, 0)), ny(2026, 11, 19, 15, 0), source="ibkr")   # Thursday's
    assert bridge_plan.chain_universe_want(sat)["symbols"] == ["AAPL", "MSFT"]
    chains.store_chain(snap(payload, sat), sat, source="ibkr")        # fetched after Friday's open
    assert bridge_plan.chain_universe_want(sat)["symbols"] == ["MSFT"]


# ----------------------------------------------------------------------------- precedence
def test_frozen_chain_creates_the_missing_row_with_the_session_close(tmpdb, payload):
    bars_repo.upsert_bars([Bar("AAPL", THU, None, None, None, 340.0, 1), Bar("AAPL", FRI, None, None, None, 350.0, 9)])
    sat = ny(2026, 11, 21, 12, 0)
    chains.store_chain(snap(payload, sat, price=355.0), sat, source="ibkr")   # 355: an extended-hours last trade
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["after_hours"] is True and m["source"] == "ibkr"
    assert m["close"] == 350.0 and m["change_pct"] == pytest.approx(350 / 340 - 1) and m["volume"] == 9
    assert m["iv30"] is not None                                       # chain-derived values of the closing quotes
    assert quotes_repo.get_quote("AAPL").price == 355.0               # latest quote: always the newest
    assert options_repo.eod_sources([FRI], ["AAPL"])[FRI] == {}       # never archived from a frozen push
    later = sat + timedelta(hours=3)
    chains.store_chain(snap(payload, later, price=356.0), later, source="ibkr")   # a newer fill refreshes a fill
    assert metrics_repo.get_metrics("AAPL", FRI)["updated_at"] > m["updated_at"]


def test_closing_snapshot_replaces_a_fill_and_a_fill_never_replaces_session_data(tmpdb, payload):
    sat = ny(2026, 11, 21, 12, 0)
    chains.store_chain(snap(payload, sat), sat, source="ibkr")
    final = ny(2026, 11, 21, 13, 0)                                   # catch-up closing snapshot (vendor sweep)
    chains.store_chain(snap(payload, final, price=348.0), final, final=True, source="vendor")
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["after_hours"] is False and m["source"] == "vendor" and m["close"] == 348.0
    assert options_repo.eod_sources([FRI], ["AAPL"])[FRI] == {"AAPL": "vendor"}
    sun = ny(2026, 11, 22, 12, 0)
    chains.store_chain(snap(payload, sun, price=400.0), sun, source="ibkr")            # frozen IBKR chain
    chains.store_underlying_stats(FRI, [stat(price=400.0, call_oi=1)], "ibkr", after_hours=True)
    assert metrics_repo.get_metrics("AAPL", FRI) == m                                  # untouched, also its NULLs
    assert quotes_repo.get_quote("AAPL").price == 400.0


def test_in_session_rows_are_never_touched_by_a_fill(tmpdb, payload):
    t = ny(2026, 11, 20, 15, 0)
    chains.store_underlying_stats(FRI, [stat("MSFT", call_volume=10)], "ibkr")          # in-session stats
    chains.store_chain(snap(payload, t), t, source="ibkr")                              # in-session chain
    before = metrics_repo.metrics_on(FRI)
    sat = ny(2026, 11, 21, 12, 0)
    assert chains.store_underlying_stats(FRI, [stat("MSFT", call_volume=99), stat("AAPL", call_oi=5)], "ibkr",
                                         after_hours=True) == 0
    chains.store_chain(snap(payload, sat, price=1.0), sat, source="ibkr")
    assert metrics_repo.metrics_on(FRI) == before
    assert before["MSFT"]["after_hours"] is False


def test_stats_fill_creates_flagged_rows_keeps_nulls_and_session_stats_replace_it(tmpdb):
    bars_repo.upsert_bars([Bar("AAPL", THU, None, None, None, 340.0, 1), Bar("AAPL", FRI, None, None, None, 350.0, 9)])
    assert chains.store_underlying_stats(FRI, [stat(), stat("F", price=12.0, iv30=0.4)], "ibkr", after_hours=True) == 2
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["after_hours"] is True and m["source"] == "ibkr:stats"
    assert m["close"] == 350.0 and m["volume"] == 9                   # the session's bar, not the frozen last
    assert m["call_volume"] == 300000 and m["call_oi"] is None and m["iv30"] is None      # unknown stays NULL
    assert metrics_repo.get_metrics("F", FRI)["close"] == 12.0        # no bar stored: the pushed price
    assert vol_history_repo.last_dates("iv30", ["F"]) == {"F": FRI}
    chains.store_underlying_stats(FRI, [stat(call_volume=1)], "ibkr", after_hours=True)  # a fill refreshes a fill
    assert metrics_repo.get_metrics("AAPL", FRI)["call_volume"] == 1
    chains.store_underlying_stats(FRI, [stat(call_volume=7)], "ibkr")                    # the session's own stats
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["after_hours"] is False and m["call_volume"] == 7 and m["close"] == 351.0


def test_stats_push_is_classified_by_its_time_and_must_be_of_its_session(tmpdb):
    from optionlens.services import ingest
    body = {"source": "ibkr", "delayed": True, "stats": [stat()]}
    ingest.underlying_stats({**body, "session_date": FRI, "ts": ny(2026, 11, 20, 19, 0)}, now=ny(2026, 11, 20, 19, 0))
    assert metrics_repo.get_metrics("AAPL", FRI)["after_hours"] is False               # inside the stats window
    ingest.underlying_stats({**body, "session_date": FRI, "ts": ny(2026, 11, 21, 12, 0),
                             "stats": [stat("F")]}, now=ny(2026, 11, 21, 12, 0))
    assert metrics_repo.get_metrics("F", FRI)["after_hours"] is True
    with pytest.raises(BadRequest, match="not of session"):            # queued before the open, sent after it
        ingest.underlying_stats({**body, "session_date": FRI, "ts": ny(2026, 11, 23, 9, 31)}, now=ny(2026, 11, 23, 9, 31))
    with pytest.raises(BadRequest, match="future"):
        ingest.underlying_stats({**body, "session_date": FRI, "ts": ny(2026, 11, 21, 12, 0)}, now=ny(2026, 11, 20, 12, 0))


def test_unusual_option_volume_interim_criterion_from_one_filled_session(tmpdb):
    from optionlens.services.reports import registry, universe
    chains.store_underlying_stats(FRI, [stat(call_volume=6000, put_volume=2000, call_oi=5000, put_oi=5000),
                                        stat("F", call_volume=9000, put_volume=0)], "ibkr", after_hours=True)
    universe.invalidate()
    r = registry.run_sync("unusual-option-volume")
    assert [x["symbol"] for x in r["rows"]] == ["AAPL"] and r["rows"][0]["criterion"] == "Vol/OI (interim)"
    assert r["after_hours"] is True
    assert any("1 symbol(s) without open interest" in n for n in r["notes"])           # F: no OI, said so
    universe.invalidate()
