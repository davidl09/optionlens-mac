"""Canada in the collectors: the CAD risk-free rate (Bank of Canada), the Canadian universe selection, Questrade daily
bars, per-market sweeps / closing snapshots / EOD, per-market live sessions, and the US-only IBKR bridge."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from optionlens import db
from optionlens.analytics import markets
from optionlens.analytics.calendar import NY
from optionlens.jobs import bridge_plan, chains, ondemand, universe_ca
from optionlens.live.session import next_live_start, session_at
from optionlens.live.subscriptions import Wanted, for_sessions
from optionlens.repo import market as market_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.services import context
from optionlens.sources import boc, chain_source
from optionlens.sources.chain_source import NotListed
from optionlens.sources.questrade.mapping import SymbolInfo
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade

FIX = Path(__file__).parent / "fixtures"


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    context.invalidate()
    yield
    db.close()
    context.invalidate()


# ----------------------------------------------------------------------------- CAD risk-free rate
def test_bank_of_canada_tbill_parser_on_a_recorded_answer():
    rows = boc.parse_observations(json.loads((FIX / "boc_tbill_3m.json").read_text()))
    assert rows[0] == (date(2026, 11, 10), pytest.approx(0.0261))
    assert date(2026, 11, 11) not in dict(rows)              # Remembrance Day: no observation published
    assert all(0 < r < 0.2 for _, r in rows) and rows == sorted(rows)
    assert boc.parse_observations({"observations": [{"d": "2026-11-23", boc.SERIES: {"v": ""}}]}) == []
    with pytest.raises(ValueError):
        boc.parse_observations({"message": "Series not found"})


def test_rates_are_kept_per_currency_and_options_use_their_market_s(tmpdb):
    market_repo.upsert_rates([(date(2026, 11, 20), 0.0426)])
    market_repo.upsert_rates([(date(2026, 11, 20), 0.0264)], "CAD")
    assert market_repo.rate_on(currency="USD") == pytest.approx(0.0426)
    assert market_repo.rate_on(currency="CAD") == pytest.approx(0.0264)
    assert context.rate_for("RY") == pytest.approx(0.0426) and context.rate_for("RY.TO") == pytest.approx(0.0264)
    assert context.rate_fn_for("RY.TO")(date(2026, 11, 23)) == pytest.approx(0.0264)


def test_cad_rate_falls_back_to_the_configured_one(tmpdb):
    from optionlens.config import get_settings
    assert context.rate_for("TD.TO") == get_settings().risk_free_rate_cad


# ----------------------------------------------------------------------------- universe selection
class Directory:
    """Questrade as the selection sees it: TSX listings by ticker, one TSX Venture listing, no options for one."""

    def __init__(self):
        def info(price, name, exchange, mcap, has_options=True, sector=None):
            return SymbolInfo(has_options, price, name=name, security_type="Stock", listing_exchange=exchange,
                              currency="CAD", market_cap=mcap, sector=sector)
        self.info = {
            "RY.TO": info(150.0, "ROYAL BANK", "TSX", 2.9e11, sector="FinancialServices"),
            "TD.TO": info(90.0, "TORONTO-DOMINION", "TSX", 1.6e11),
            "SMALL.TO": info(5.0, "SMALL CO", "TSX", 1e8),
            "NOOPT.TO": info(50.0, "NO OPTIONS", "TSX", 9e11, has_options=False),
            "VEN.VN": info(1.0, "VENTURE CO", "TSXV", 5e9),
            "XIU.TO": info(40.0, "ISHARES S&P/TSX 60", "TSX", None),
        }
        self.asked: list[str] = []

    async def symbol_info(self, symbol):
        self.asked.append(symbol)
        if symbol not in self.info:
            raise NotListed(symbol)
        return self.info[symbol]

    async def fetch_daily(self, symbol, start, end):
        return []


@pytest.fixture
def directory(tmpdb, tmp_path, monkeypatch):
    f = tmp_path / "canada.txt"
    f.write_text("# test candidates\nRY stock # Royal Bank\nTD stock\nSMALL stock\nNOOPT stock\nVEN stock\n"
                 "GONE stock # not on Questrade\nXIU etf # iShares\n")
    d = Directory()
    universe_ca.candidates.cache_clear()
    monkeypatch.setattr(universe_ca, "directory", lambda: d)
    monkeypatch.setattr(universe_ca, "get_settings",
                        lambda: type("S", (), {"ca_file": f, "universe_ca_stocks": 2})())
    yield d
    universe_ca.candidates.cache_clear()


async def test_canadian_universe_is_the_largest_optionable_stocks_plus_etfs(directory):
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    symbols_repo.set_universe(["AAPL"])
    detail = await universe_ca.universe_ca()
    members = set(symbols_repo.universe_symbols(include_unoptionable=True))
    # top 2 optionable stocks by market cap (RY 290B, TD 160B; NOOPT is bigger but lists no options), all ETFs
    assert members == {"AAPL", "RY.TO", "TD.TO", "XIU.TO"}
    assert "VEN.TO" in directory.asked and "VEN.VN" in directory.asked      # TSX first, then TSX Venture
    assert "1 not listed" in detail and "3 stocks, 1 ETFs" not in detail and "2 stocks, 1 ETFs" in detail
    row = symbols_repo.get_symbol("RY.TO")
    assert (row["kind"], row["sector"], row["name"]) == ("stock", "Financial Services", "ROYAL BANK")
    assert symbols_repo.get_symbol("XIU.TO")["kind"] == "etf"
    # the US universe job does not drop the Canadian members, and vice versa
    symbols_repo.set_universe(["AAPL", "MSFT"])
    assert {"RY.TO", "TD.TO", "XIU.TO"} <= set(symbols_repo.universe_symbols(include_unoptionable=True))


async def test_a_partial_refresh_keeps_the_stored_canadian_universe(directory):
    await universe_ca.universe_ca()
    before = set(symbols_repo.universe_symbols(include_unoptionable=True))

    async def flaky(symbol):
        raise RuntimeError("503 from Questrade")
    directory.symbol_info = flaky
    with pytest.raises(RuntimeError, match="kept"):
        await universe_ca.universe_ca()
    assert set(symbols_repo.universe_symbols(include_unoptionable=True)) == before


async def test_canadian_universe_is_skipped_without_questrade(tmpdb, monkeypatch):
    monkeypatch.setattr(universe_ca, "directory", lambda: None)
    assert (await universe_ca.universe_ca()).startswith("skipped: Questrade is not connected")


def test_the_bundled_candidates_are_montreal_exchange_classes():
    universe_ca.candidates.cache_clear()
    c = {x.ticker: x for x in universe_ca.candidates()}
    assert {"RY", "TD", "ENB", "CNQ", "SHOP", "TECK.B"} <= set(c)
    assert {"XIU", "XIC", "XEG", "XFN", "ZEB", "HXT"} <= {t for t, x in c.items() if x.kind == "etf"}
    assert not [t for t in c if t[-1].isdigit()]              # adjusted classes (TRP1, ...) left out
    assert len([x for x in c.values() if x.kind == "stock"]) >= 200


# ----------------------------------------------------------------------------- Questrade daily bars
async def test_questrade_daily_bars_continue_past_the_candle_limit(tmp_path, monkeypatch):
    from optionlens.sources.questrade import source as source_mod
    monkeypatch.setattr(source_mod, "MAX_CANDLES", 50)        # a full answer (the documented 2,000) is continued
    fake = FakeQuestrade()
    fake.listed["RY.TO"] = 1000
    fake.candle_cap = 50
    src = QuestradeSource(tmp_path, 8, 0.25, fake.http)
    await src.tokens.connect("pasted-token")
    bars = await src.fetch_daily("RY.TO", date(2026, 6, 1), date(2026, 11, 20))
    assert bars[0].date == date(2026, 6, 1) and bars[-1].date == date(2026, 11, 20)
    assert len(bars) == len({b.date for b in bars}) == sum(
        1 for d in range((date(2026, 11, 20) - date(2026, 6, 1)).days + 1)
        if date.fromordinal(date(2026, 6, 1).toordinal() + d).weekday() < 5)
    assert fake.path_count("/v1/markets/candles/1000") >= 3 and all(b.symbol == "RY.TO" for b in bars)
    assert all(r.url.params["interval"] == "OneDay" for r in fake.requests if "candles" in r.url.path)


async def test_toronto_symbols_resolve_to_the_tsx_listing(tmp_path):
    fake = FakeQuestrade()
    fake.listed["RY.TO"] = 1000
    src = QuestradeSource(tmp_path, 8, 0.25, fake.http)
    await src.tokens.connect("pasted-token")
    assert await src.symbol_id("RY.TO") == 1000
    with pytest.raises(NotListed):
        await src.symbol_id("RY")                              # the NYSE listing is a different symbol


# ----------------------------------------------------------------------------- per-market sweeps and EOD
class Recorder:
    name = "questrade"

    def __init__(self):
        self.calls = []

    def connected(self):
        return True

    def unavailable(self):
        return None

    def problem(self):
        return None

    async def keepalive(self):
        return None

    def status(self):
        return {}

    async def fetch_chain(self, symbol, final=False):
        self.calls.append((symbol, final))
        raise NotListed(symbol)


@pytest.fixture
def recorder(tmpdb, monkeypatch):
    symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"} for s in ("AAPL", "RY.TO")])
    symbols_repo.set_universe(["AAPL"])
    symbols_repo.set_universe(["RY.TO"], markets.CA)
    r = Recorder()
    monkeypatch.setitem(chain_source.REGISTRY, "questrade", lambda: r)
    return r


@pytest.mark.parametrize("day,swept,skipped", [
    (date(2026, 5, 18), "AAPL", "RY.TO"),        # Victoria Day: TSX closed, NYSE open
    (date(2026, 7, 3), "RY.TO", "AAPL"),         # Independence Day observed: NYSE closed, TSX open
])
async def test_sweeps_leave_out_the_market_that_is_closed(recorder, monkeypatch, day, swept, skipped):
    monkeypatch.setattr(chains, "now_ny", lambda: datetime.combine(day, datetime.min.time(), NY).replace(hour=11))
    await chains.sweep()
    asked = {s for s, _ in recorder.calls}
    assert swept in asked and skipped not in asked


def test_closing_snapshots_per_market_and_once_per_session():
    # US Thanksgiving Friday: NYSE closes at 13:00, the TSX at 16:00
    assert [m.code for m, _ in chains.closing_markets(ny(2026, 11, 27, 13, 25))] == ["US"]
    chains._final_done["US"] = date(2026, 11, 27)
    assert [m.code for m, _ in chains.closing_markets(ny(2026, 11, 27, 16, 25))] == ["CA"]
    assert [m.code for m, _ in chains.closing_markets(ny(2026, 5, 18, 16, 25))] == ["US"]     # Victoria Day
    assert chains.closing_markets(ny(2026, 11, 27, 20, 0)) == []                              # deadline


async def test_eod_closes_only_the_markets_that_traded(recorder, monkeypatch):
    monkeypatch.setattr(chains, "now_ny", lambda: ny(2026, 7, 3, 20, 5))       # NYSE holiday, TSX session
    with pytest.raises(chains.NothingArchived, match="2026-07-03"):          # Canada is followed: its archive is owed
        await chains.eod()
    symbols_repo.set_universe([], markets.CA)                                 # no Canadian symbols: nothing to close
    assert (await chains.eod()) == "skipped: no symbols on the markets that closed 2026-07-03"


# ----------------------------------------------------------------------------- live sessions
def test_canadian_live_session_has_no_extended_hours():
    assert session_at(ny(2026, 11, 24, 8, 0), "US") == "extended" and session_at(ny(2026, 11, 24, 8, 0), "CA") == "closed"
    assert session_at(ny(2026, 11, 24, 10, 0), "CA") == "regular"
    assert session_at(ny(2026, 11, 24, 16, 30), "CA") == "closed"
    assert session_at(ny(2026, 5, 18, 10, 0), "CA") == "closed" and session_at(ny(2026, 5, 18, 10, 0)) == "regular"
    assert next_live_start(ny(2026, 5, 18, 10, 0), "CA") == ny(2026, 5, 19, 9, 30)


def test_live_set_follows_each_market_s_session():
    w = Wanted(frozenset({"AAPL", "RY.TO"}), {"AAPL261218C00200000": "AAPL", "RY.TO261218C00120000": "RY.TO"})
    victoria = for_sessions(w, {"US": "regular", "CA": "closed"})
    assert victoria.symbols == {"AAPL"} and set(victoria.contracts) == {"AAPL261218C00200000"}
    premarket = for_sessions(w, {"US": "extended", "CA": "closed"})
    assert premarket.symbols == {"AAPL"} and not premarket.contracts


# ----------------------------------------------------------------------------- IBKR bridge: US only
def test_bridge_is_never_asked_for_canadian_listings(tmpdb):
    assert chain_source.bridge_serves("RY") and not chain_source.bridge_serves("RY.TO")
    assert bridge_plan.bridge_symbols(["AAPL", "RY.TO", "SPX", "AP.UN.TO"]) == ["AAPL", "SPX"]
    from optionlens.repo import user as user_repo
    user_repo.create_watchlist("w", ["RY.TO", "AAPL"])
    assert "RY.TO" not in bridge_plan.chain_universe(ny(2026, 11, 24, 10, 0))
    symbols_repo.upsert_symbols([{"symbol": s, "kind": "stock"} for s in ("AAPL", "RY.TO")])
    symbols_repo.set_universe(["AAPL"])
    symbols_repo.set_universe(["RY.TO"], markets.CA)
    from optionlens.services import ingest
    for now in (ny(2026, 11, 24, 10, 0), ny(2026, 11, 24, 17, 0), ny(2026, 11, 28, 12, 0)):
        w = json.dumps(ingest.wants(now), default=str)
        assert "AAPL" in w and "RY.TO" not in w                  # every IBKR work list is US-only


async def test_nasdaq_is_never_asked_for_canadian_listings(tmpdb):
    assert await ondemand.fetch_cached("summary", "T.TO") is None     # TELUS; Nasdaq's T is AT&T
    assert not ondemand.covered_by_nasdaq("T.TO") and ondemand.covered_by_nasdaq("T")
