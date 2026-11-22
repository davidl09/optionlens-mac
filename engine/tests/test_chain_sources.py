"""Source tags and precedence of stored chains, pushed underlying stats, reconstructed archives, the
bridge's wants plan and the `source` migration (offline)."""
from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from optionlens import db
from optionlens.analytics import bs
from optionlens.analytics.calendar import NY, year_fraction_to_expiry
from optionlens.jobs import bridge_plan, catchup, chains, ondemand, prices
from optionlens.models import Bar
from optionlens.repo import bars as bars_repo
from optionlens.repo import metrics as metrics_repo
from optionlens.repo import options as options_repo
from optionlens.repo import quotes as quotes_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.repo import user as user_repo
from optionlens.repo import views as views_repo
from optionlens.repo import vol_history as vol_history_repo
from optionlens.sources import cboe

FIX = Path(__file__).parent / "fixtures"
FRI = date(2026, 11, 20)


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    yield
    db.close()


@pytest.fixture(scope="module")
def payload():
    return json.loads((FIX / "cboe_chain_AAPL.json").read_text())


def snap(payload, fetched: datetime, price: float | None = None):
    s = cboe.parse_chain(payload, "AAPL", fetched_at=fetched)
    return s if price is None else dataclasses.replace(s, quote=dataclasses.replace(s.quote, price=price))


def eod_source(day: date = FRI) -> str | None:
    return options_repo.eod_sources([day], ["AAPL"])[day].get("AAPL")


# ----------------------------------------------------------------------------- precedence
def test_equal_closing_chains_the_fresher_wins_everywhere(tmpdb, payload):
    """No source is preferred: of two equal closing snapshots the fresher one is the served chain, the session's
    metrics row and its archive; every row keeps its source."""
    t1, t2 = ny(2026, 11, 20, 16, 5), ny(2026, 11, 20, 16, 25)
    chains.store_chain(snap(payload, t1), t1, final=True, source="ibkr")
    assert eod_source() == "ibkr" and metrics_repo.source_of(FRI, "AAPL") == "ibkr"
    assert options_repo.latest_source("AAPL") == "ibkr"
    chains.store_chain(snap(payload, t2, price=400.0), t2, final=True, source="vendor")
    assert eod_source() == "vendor" and metrics_repo.source_of(FRI, "AAPL") == "vendor"
    assert options_repo.latest_source("AAPL") == "vendor"
    assert set(options_repo.eod_for_date(FRI, ["AAPL"])["underlying"]) == {400.0}
    assert quotes_repo.get_quote("AAPL").price == 400.0 and quotes_repo.source_of("AAPL") == "vendor"
    options_repo.write_option_eod(FRI, t1, ["AAPL"])                              # the bulk archive: same chain
    assert eod_source() == "vendor"


def test_an_older_snapshot_pushed_later_does_not_replace_a_fresher_equal_one(tmpdb, payload):
    t1, t2 = ny(2026, 11, 20, 16, 5), ny(2026, 11, 20, 16, 25)
    chains.store_chain(snap(payload, t2, price=400.0), t2, final=True, source="vendor")
    chains.store_chain(snap(payload, t1), t2, final=True, source="ibkr")         # older snapshot, pushed later
    assert eod_source() == "vendor" and metrics_repo.source_of(FRI, "AAPL") == "vendor"
    assert options_repo.latest_source("AAPL") == "vendor"
    assert quotes_repo.get_quote("AAPL").price == 400.0                            # the newest quote stays


def test_intraday_chains_far_apart_the_fresher_wins(tmpdb, payload):
    t1, t2 = ny(2026, 11, 20, 11, 0), ny(2026, 11, 20, 12, 0)
    chains.store_chain(dataclasses.replace(snap(payload, t1), delayed=False), t1, source="ibkr")
    chains.store_chain(snap(payload, t2, price=400.0), t2, source="vendor")      # delayed-unknown, an hour later
    assert metrics_repo.get_metrics("AAPL", FRI)["close"] == 400.0 and options_repo.latest_source("AAPL") == "vendor"
    t3 = ny(2026, 11, 20, 12, 5)                                                  # within the same snapshot window:
    chains.store_chain(dataclasses.replace(snap(payload, t3, price=401.0), delayed=False), t3, source="ibkr")
    assert metrics_repo.get_metrics("AAPL", FRI)["close"] == 401.0 and options_repo.latest_source("AAPL") == "ibkr"
    t4 = ny(2026, 11, 20, 12, 10)                                                 # real-time beats a fresher delayed
    chains.store_chain(dataclasses.replace(snap(payload, t4, price=402.0), delayed=True), t4, source="vendor")
    assert options_repo.latest_source("AAPL") == "ibkr" and metrics_repo.source_of(FRI, "AAPL") == "ibkr"
    assert eod_source() is None                                                    # non-final: no archive


def test_a_later_session_always_replaces_the_served_chain(tmpdb, payload):
    fri, mon = ny(2026, 11, 20, 16, 25), ny(2026, 11, 23, 9, 35)
    chains.store_chain(dataclasses.replace(snap(payload, fri), delayed=False), fri, final=True, source="questrade")
    chains.store_chain(dataclasses.replace(snap(payload, mon), delayed=True), mon, source="ibkr")
    assert options_repo.latest_source("AAPL") == "ibkr" and eod_source() == "questrade"


async def test_eod_without_vendor_archives_unarchived_pushes_only(tmpdb, payload, monkeypatch):
    symbols_repo.upsert_symbols([{"symbol": "MSFT", "kind": "stock"}])
    t = ny(2026, 11, 20, 16, 30)
    chains.store_chain(snap(payload, t), t, final=True, source="ibkr")             # archived when pushed
    aapl = snap(payload, t)
    msft = dataclasses.replace(aapl, quote=dataclasses.replace(aapl.quote, symbol="MSFT"), contracts=tuple(
        dataclasses.replace(c, symbol="MSFT", osi="MSFT" + c.osi[4:]) for c in aapl.contracts))  # pushed, not final
    chains.store_chain(msft, t, source="ibkr")
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 11, 20, 20, 5))
    before = len(options_repo.eod_for_date(FRI, ["AAPL"])["osi"])
    detail = await chains.eod()
    assert detail.startswith("2026-11-20:") and "sweep" not in detail
    assert options_repo.eod_sources([FRI])[FRI] == {"AAPL": "ibkr", "MSFT": "ibkr"}
    assert len(options_repo.eod_for_date(FRI, ["AAPL"])["osi"]) == before
    assert set(bars_repo.bars_on(FRI)) == {"AAPL", "MSFT"}


async def test_a_trading_day_eod_that_archives_nothing_fails(tmpdb, monkeypatch):
    """No closing chain pushed (bridge off after the close): the run is a failure with the reason, never OK with
    '0 option_eod rows, 0 bars'."""
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 11, 20, 20, 5))
    with pytest.raises(chains.NothingArchived, match="2026-11-20: nothing archived"):
        await chains.eod()


async def test_visit_records_the_view_and_fetches_no_chain(tmpdb, payload, monkeypatch):
    async def no_history(row, start=None):
        return 0
    monkeypatch.setattr(prices, "backfill_symbol", no_history)
    ondemand._viewed.clear()
    row = await ondemand.visit("AAPL")
    assert row["symbol"] == "AAPL" and options_repo.latest_fetched_at("AAPL") is None
    assert views_repo.viewed_since(ny(2026, 1, 1)) == ["AAPL"]


# ----------------------------------------------------------------------------- underlying stats
STAT = {"symbol": "AAPL", "price": 349.1, "change_pct": -0.0098, "volume": 4.1e7, "call_volume": 338000,
        "put_volume": 251000, "call_oi": 2960000, "put_oi": 2730000, "iv30": 0.251, "hv30": 0.27}


def test_stats_create_row_fill_chain_row_and_feed_iv_history(tmpdb, payload):
    symbols_repo.upsert_symbols([{"symbol": "XYZ", "kind": "stock"}])
    n = chains.store_underlying_stats(FRI, [STAT, {"symbol": "XYZ", "price": 10.0, "iv30": None}], "ibkr")
    assert n == 2
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["source"] == "ibkr:stats" and m["call_volume"] == 338000 and m["close"] == 349.1
    assert m["iv30"] == pytest.approx(0.251) and m["skew25_30"] is None
    assert "source" not in metrics_repo.metric_columns()
    assert vol_history_repo.last_dates("iv30", ["AAPL", "XYZ"]) == {"AAPL": FRI}
    chains.store_underlying_stats(FRI, [{**STAT, "call_volume": 400000}], "ibkr")   # newer stats replace stats
    assert metrics_repo.get_metrics("AAPL", FRI)["call_volume"] == 400000
    # a chain replaces the stats values it can derive and keeps the rest
    t = ny(2026, 11, 20, 15, 0)
    chains.store_chain(snap(payload, t), t, source="ibkr")
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["source"] == "ibkr" and m["call_volume"] != 400000 and m["skew25_30"] is not None
    assert m["hv30"] == pytest.approx(0.27)                                        # no closes: kept from stats
    # stats after a chain only fill what the chain left NULL
    chains.store_underlying_stats(FRI, [{**STAT, "call_volume": 1, "hv60": None}], "ibkr")
    m2 = metrics_repo.get_metrics("AAPL", FRI)
    assert m2["call_volume"] == m["call_volume"] and m2["source"] == "ibkr"


def test_stats_hv_from_stored_closes(tmpdb):
    d, bars = FRI - timedelta(days=60), []
    while d < FRI:
        if d.weekday() < 5:
            bars.append(Bar("AAPL", d, None, None, None, 100.0 * (1.01 if len(bars) % 2 else 0.99), 1))
        d += timedelta(days=1)
    bars_repo.upsert_bars(bars)
    chains.store_underlying_stats(FRI, [{"symbol": "AAPL", "price": 100.0}], "ibkr")
    m = metrics_repo.get_metrics("AAPL", FRI)
    assert m["hv20"] is not None and m["hv20"] > 0.1 and m["hv252"] is None


# ----------------------------------------------------------------------------- reconstruction
def _bar(day, strike, right, close, volume=10, expiry=date(2026, 12, 18)):
    return {"expiry": expiry, "strike": strike, "right": right, "date": day, "open": None, "high": None,
            "low": None, "close": close, "volume": volume}


def test_reconstruct_rows_math():
    day, spot, r, q, k = date(2026, 11, 18), 100.0, 0.04, 0.01, 105.0
    t = year_fraction_to_expiry(date(2026, 12, 18), datetime.combine(day, datetime.min.time(), NY).replace(hour=16))
    c = bs.price(spot, k, t, r, q, 0.32, "C")
    rows = catchup.reconstruct_rows("AAPL", day, [_bar(day, k, "C", c), _bar(day, 120.0, "P", 1.0),
                                                  _bar(day, 90.0, "P", 1.0, volume=0),
                                                  _bar(day, 90.0, "C", 1.0, expiry=date(2026, 11, 13))], spot, r, q)
    assert [x["osi"] for x in rows] == ["AAPL261218C00105000", "AAPL261218P00120000"]
    assert rows[0]["iv"] == pytest.approx(0.32, abs=1e-6)
    assert rows[0]["delta"] == pytest.approx(bs.delta(spot, k, t, r, q, 0.32, "C"), abs=1e-6)
    assert rows[0]["last"] == pytest.approx(c) and rows[0]["underlying"] == spot
    assert (rows[0]["iv_source"], rows[0]["greeks_source"]) == ("computed_last", "computed")
    assert rows[1]["iv"] is None and rows[1]["delta"] is None                      # below intrinsic
    assert (rows[1]["iv_source"], rows[1]["greeks_source"]) == (None, None)
    assert catchup.reconstruct_rows("AAPL", day, [_bar(day, k, "C", c)], None, r, q)[0]["iv"] is None


def test_store_reconstructed_bars_never_overwrites_real_rows(tmpdb, payload):
    thu, wed = date(2026, 11, 19), date(2026, 11, 18)
    bars_repo.upsert_bars([Bar("AAPL", wed, None, None, None, 350.0, 1), Bar("AAPL", thu, None, None, None, 351.0, 1)])
    t = ny(2026, 11, 19, 16, 30)
    chains.store_chain(snap(payload, t), t, final=True, source="ibkr")             # real archive on Thursday
    real = options_repo.eod_for_date(thu, ["AAPL"])
    n = catchup.store_reconstructed_bars("AAPL", [_bar(wed, 350.0, "C", 12.0), _bar(wed, 340.0, "P", 6.0),
                                                  _bar(thu, 350.0, "C", 99.0)], "ibkr")
    assert n == 2 and eod_source(wed) == "reconstructed" and eod_source(thu) == "ibkr"
    assert len(options_repo.eod_for_date(thu, ["AAPL"])["osi"]) == len(real["osi"])
    rows = options_repo.eod_for_date(wed, ["AAPL"])
    assert math.isnan(rows["bid"][0]) and math.isnan(rows["oi"][0]) and rows["underlying"][0] == 350.0
    assert 0.1 < rows["iv"][0] < 1.0
    # a repeated push replaces that contract and keeps the others (the bridge pushes contract by contract);
    # OI comparisons skip reconstructions
    assert catchup.store_reconstructed_bars("AAPL", [_bar(wed, 350.0, "C", 12.5)], "ibkr") == 1
    assert catchup.store_reconstructed_bars("AAPL", [_bar(wed, 345.0, "C", 14.0)], "ibkr") == 1
    assert options_repo.prior_eod_date(FRI, "AAPL") == thu
    assert options_repo.prior_eod_date(thu, "AAPL") is None and options_repo.prior_oi(thu, "AAPL") == {}
    rows = options_repo.eod_for_date(wed, ["AAPL"])
    assert dict(zip(rows["osi"].tolist(), rows["last"].tolist())) == {
        "AAPL261218C00345000": 14.0, "AAPL261218C00350000": 12.5, "AAPL261218P00340000": 6.0}
    # a partial (reconstructed) archive is still offered for catch-up; a real one is not
    assert catchup.missing_eod(["AAPL"], [wed, thu]) == {"AAPL": [wed]}


# ----------------------------------------------------------------------------- bridge plan
def test_catchup_sessions():
    s = catchup.catchup_sessions(ny(2026, 11, 20, 19, 0), 3)                       # Friday's push window open
    assert s == [date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19)]
    assert catchup.catchup_sessions(ny(2026, 11, 20, 20, 0), 2) == [date(2026, 11, 19), FRI]
    assert catchup.catchup_sessions(ny(2026, 11, 28, 9, 0), 2) == [date(2026, 11, 25), date(2026, 11, 27)]


def test_chain_universe_and_wants(tmpdb, payload, monkeypatch):
    for s in ("MSFT", "TSLA", "NVDA", "F"):
        symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"}])
    symbols_repo.set_universe(["AAPL", "MSFT", "TSLA", "NVDA", "F"])
    user_repo.create_watchlist("w", ["F"])
    now = ny(2026, 11, 20, 20, 30)
    views_repo.record_view("TSLA", now - timedelta(days=1))
    views_repo.record_view("OLD", now - timedelta(days=30))
    for sym, v in (("NVDA", 900), ("AAPL", 500), ("MSFT", 100)):
        metrics_repo.upsert_metrics(date(2026, 11, 19), sym, {"call_volume": v, "put_volume": v})
    monkeypatch.setattr(bridge_plan, "get_settings", lambda: type("S", (), {"chain_universe_size": 2})())
    assert bridge_plan.chain_universe(now) == ["F", "TSLA", "NVDA", "AAPL"]
    symbols_repo.set_optionable("TSLA", False)                                     # IBKR lists no options
    assert bridge_plan.chain_universe(now) == ["F", "NVDA", "AAPL"]
    symbols_repo.set_optionable("TSLA", True)
    live = bridge_plan.chain_universe_want(ny(2026, 11, 20, 19, 59))
    assert live == {"symbols": ["F", "TSLA", "NVDA", "AAPL"], "max_age_s": 3600, "expiries": 6, "moneyness": 0.20}
    # after the window (20:30 ET), before it (08:59 ET) and on Saturday: only an after-hours fill of the current
    # session (none of these symbols has a chain fetched since its open)
    cu = bridge_plan.chain_universe_want(now)
    assert cu["expiries"] == 6 and cu["moneyness"] == 0.20 and cu["max_age_s"] == bridge_plan.FILL_MAX_AGE_S
    assert cu["symbols"] == ["F", "TSLA", "NVDA", "AAPL"] and cu["as_of_session"] == FRI
    assert bridge_plan.chain_universe_want(ny(2026, 11, 20, 8, 59))["as_of_session"] == date(2026, 11, 19)
    assert bridge_plan.chain_universe_want(ny(2026, 11, 21, 12, 0))["as_of_session"] == FRI   # Saturday
    # eod chains: chain universe + indices, minus real-time closing chains already archived
    t_close = ny(2026, 11, 20, 16, 30)
    chains.store_chain(snap(payload, t_close), t_close, final=True, source="ibkr")                # delayed/unknown
    assert "AAPL" in bridge_plan.eod_chains_want(ny(2026, 11, 20, 17, 0))["symbols"]             # could be beaten
    chains.store_chain(dataclasses.replace(snap(payload, t_close), delayed=False), t_close, final=True, source="ibkr")
    e = bridge_plan.eod_chains_want(ny(2026, 11, 20, 17, 0))
    assert e["session_date"] == FRI and "AAPL" not in e["symbols"] and {"SPX", "NDX", "RUT", "XSP", "F"} <= set(e["symbols"])
    assert e["not_before"] == ny(2026, 11, 20, 16, 20) and e["deadline"] == ny(2026, 11, 20, 20, 0)
    # underlying stats: the universe during the session window; outside it an after-hours fill (test_fill_wants)
    us = bridge_plan.underlying_stats_want(ny(2026, 11, 20, 11, 0))
    assert set(us["symbols"]) == {"AAPL", "MSFT", "TSLA", "NVDA", "F"} and "as_of_session" not in us
    assert bridge_plan.underlying_stats_want(ny(2026, 11, 21, 11, 0))["as_of_session"] == FRI
    # option bars: sessions lacking an archive, only where the underlying close is stored; bounded offers
    for s in ("F", "TSLA", "NVDA", "AAPL"):
        bars_repo.upsert_bars([Bar(s, d, None, None, None, 10.0, 1) for d in (date(2026, 11, 18), date(2026, 11, 19), FRI)])
    monkeypatch.setattr(catchup, "LOOKBACK_SESSIONS", 3)
    monkeypatch.setattr(catchup.catchup_sessions, "__defaults__", (None, 3))
    wants = bridge_plan.option_bars_want(now)
    want = {w["symbol"]: w for w in wants}
    assert set(want) == {"F", "TSLA", "NVDA", "AAPL"}
    assert want["F"]["sessions"] == [date(2026, 11, 18), date(2026, 11, 19), FRI] and want["F"]["moneyness"] == 0.15
    assert want["AAPL"]["sessions"] == [date(2026, 11, 18), date(2026, 11, 19)]     # Friday archived
    # Reading the want counts nothing: an offer counts only once the bridge accepted it (ack-wants).
    assert bridge_plan.option_bars_want(now + timedelta(minutes=5)) == wants
    assert bridge_plan.record_offers(wants, now) == 11
    assert bridge_plan.option_bars_want(now + timedelta(minutes=5)) == []           # just accepted
    later = now + bridge_plan.REOFFER_AFTER
    assert len(bridge_plan.option_bars_want(later)) == 4
    bridge_plan.record_offers(bridge_plan.option_bars_want(later), later)
    bridge_plan.record_offers(bridge_plan.option_bars_want(later + bridge_plan.REOFFER_AFTER), later + bridge_plan.REOFFER_AFTER)
    assert bridge_plan.option_bars_want(later + 3 * bridge_plan.REOFFER_AFTER) == []  # MAX_OFFERS reached


# ----------------------------------------------------------------------------- migration
OLD_TABLES = """
CREATE TABLE quotes_latest (symbol VARCHAR PRIMARY KEY, ts TIMESTAMP NOT NULL, price DOUBLE, change DOUBLE,
    change_pct DOUBLE, open DOUBLE, high DOUBLE, low DOUBLE, prev_close DOUBLE, volume BIGINT, bid DOUBLE,
    ask DOUBLE, iv30 DOUBLE);
CREATE TABLE option_latest (symbol VARCHAR NOT NULL, osi VARCHAR PRIMARY KEY, expiry DATE NOT NULL,
    strike DOUBLE NOT NULL, "right" VARCHAR NOT NULL, bid DOUBLE, ask DOUBLE, last DOUBLE, change DOUBLE,
    volume BIGINT, oi BIGINT, iv DOUBLE, delta DOUBLE, gamma DOUBLE, theta DOUBLE, vega DOUBLE, rho DOUBLE,
    theo DOUBLE, ts TIMESTAMP NOT NULL);
CREATE TABLE option_eod (date DATE NOT NULL, symbol VARCHAR NOT NULL, osi VARCHAR NOT NULL,
    expiry DATE NOT NULL, strike DOUBLE NOT NULL, "right" VARCHAR NOT NULL, bid DOUBLE, ask DOUBLE,
    last DOUBLE, volume BIGINT, oi BIGINT, iv DOUBLE, delta DOUBLE, gamma DOUBLE, theta DOUBLE, vega DOUBLE,
    underlying DOUBLE);
CREATE TABLE metrics_daily (date DATE NOT NULL, symbol VARCHAR NOT NULL, close DOUBLE, iv30 DOUBLE,
    updated_at TIMESTAMP, PRIMARY KEY (date, symbol));
INSERT INTO quotes_latest (symbol, ts, price) VALUES ('AAPL', '2026-11-19 21:00:00', 348.5);
INSERT INTO option_latest VALUES ('AAPL', 'AAPL261218C00350000', '2026-12-18', 350, 'C', 9.1, 9.3, 9.2, 0,
    10, 100, 0.25, 0.5, 0.01, -0.1, 0.4, 0.1, 9.2, '2026-11-19 21:25:00');
INSERT INTO option_eod VALUES ('2026-11-19', 'AAPL', 'AAPL261218C00350000', '2026-12-18', 350, 'C', 9.1, 9.3,
    9.2, 10, 100, 0.25, 0.5, 0.01, -0.1, 0.4, 348.5);
INSERT INTO metrics_daily (date, symbol, close) VALUES ('2026-11-19', 'AAPL', 348.5);
"""


def test_source_migration_tags_existing_rows_cboe(tmp_path, payload):
    """A database from before the source columns (pre-change schema of these tables, with rows) gains
    `source`: 'cboe' on existing rows, NULL default for new rows, and a second connect is a no-op."""
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(OLD_TABLES)
    con.close()
    db.close()
    try:
        for _ in range(2):                                                         # idempotent
            db.connect(path)
            cur = db.cursor()
            for t in ("quotes_latest", "option_latest", "option_eod", "metrics_daily"):
                assert cur.execute(f"SELECT DISTINCT source FROM {t}").fetchall() == [("cboe",)], t
            db.close()
        db.connect(path)
        assert options_repo.prior_eod_date(FRI, "AAPL") == date(2026, 11, 19)
        assert quotes_repo.get_quote("AAPL").price == 348.5 and metrics_repo.source_of(date(2026, 11, 19), "AAPL") == "cboe"
        db.cursor().execute("INSERT INTO quotes_latest (symbol, ts) VALUES ('ZZZ', now())")
        assert quotes_repo.source_of("ZZZ") is None                               # no default for new rows
    finally:
        db.close()


# ----------------------------------------------------------------------------- report notes
def test_oi_changes_note_reports_gaps(tmpdb, payload, monkeypatch):
    from optionlens.services.reports import options as opt_reports
    from optionlens.services.reports.base import RunParams
    t = ny(2026, 11, 16, 16, 30)
    chains.store_chain(snap(payload, t), t, final=True, source="ibkr")             # Monday's archive
    metrics_repo.upsert_metrics(FRI, "AAPL", {"close": 1.0})                       # current session: Friday
    monkeypatch.setattr(opt_reports, "today", lambda: FRI)
    p = RunParams(min_change=0, side="both", dte_min=0, dte_max=730)
    notes = opt_reports.run_oi_changes(p).notes
    assert notes[0] == "OI change vs end-of-day 2026-11-16." and "4 sessions old" in notes[1]
    metrics_repo.upsert_metrics(date(2026, 11, 17), "AAPL", {"close": 1.0})
    monkeypatch.setattr(metrics_repo, "latest_date", lambda: date(2026, 11, 17))
    assert len(opt_reports.run_oi_changes(p).notes) == 1                           # consecutive sessions: no gap
    from optionlens.services.reports.columns import NOTE_CHAIN_UNIVERSE
    assert NOTE_CHAIN_UNIVERSE in opt_reports.OI_CHANGES.notes
