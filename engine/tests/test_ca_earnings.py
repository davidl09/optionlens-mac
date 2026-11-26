"""Canadian listings' earnings (docs/INTEGRATION.md §12 "Earnings sources"): TMX Money and Yahoo Finance parsers, the
first-answer-wins selection with per-source health, the earnings_ca / earnings_backfill_ca jobs, and what the pages
say when no source has a date.

Fixtures: tmx_earnings_2024-12-04.json, tmx_earnings_empty_day.json and tmx_wsh_events.json are RECORDED answers of
app-money.tmx.com (2026-11-26, trimmed); yahoo_quote_summary_RY.TO.json is NOT recorded (Yahoo refused the honest
client with 429) but built from Yahoo's published answer shape, as its `_fixture_note` says."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from optionlens import db, net
from optionlens.jobs import calendars, scheduler
from optionlens.models import EarningsEvent
from optionlens.net import FetchError
from optionlens.repo import earnings as earn_repo
from optionlens.repo import symbols as symbols_repo
from optionlens.services import context as ctx
from optionlens.services import symbol_earnings, system
from optionlens.sources import ca_earnings, tmx, yahoo
from optionlens.analytics.calendar import NY

FIX = Path(__file__).parent / "fixtures"
# captured at import, before the suite's offline guards (and tests/api's session-wide net.get stand-in) replace them
_REAL_TMX_QUERY, _REAL_YAHOO_SUMMARY, _REAL_NET_GET = tmx._query, yahoo._summary, net.get
TODAY = date(2026, 11, 26)


def _json(name: str):
    return json.loads((FIX / name).read_text())


def ny(*a):
    return datetime(*a, tzinfo=NY)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    ctx.invalidate()
    yield
    db.close()
    ctx.invalidate()


# ----------------------------------------------------------------------------- symbol formats
def test_symbol_mapping():
    assert [tmx.ticker(s) for s in ("RY.TO", "RCI.B.TO", "AP.UN.TO", "XYZ.VN")] == ["RY", "RCI.B", "AP.UN", "XYZ"]
    assert [yahoo.yahoo_symbol(s) for s in ("RY.TO", "RCI.B.TO", "AP.UN.TO", "XYZ.VN")] == \
        ["RY.TO", "RCI-B.TO", "AP-UN.TO", "XYZ.V"]
    for bad in ("RY", "BRK.B"):           # US listings never go to these sources
        with pytest.raises(ValueError):
            tmx.ticker(bad)
        with pytest.raises(ValueError):
            yahoo.yahoo_symbol(bad)


# ----------------------------------------------------------------------------- TMX parsers
def test_tmx_calendar_day():
    d = date(2024, 12, 4)
    evs = tmx.parse_earnings_day(_json("tmx_earnings_2024-12-04.json"), d, lambda t: None if t == "BKMT" else f"{t}.TO")
    by = {e.symbol: e for e in evs}
    assert "BKMT.TO" not in by and len(evs) == 7                              # to_symbol None: left out
    ry = by["RY.TO"]
    assert (ry.date, ry.timing, ry.eps_estimate, ry.eps_actual, ry.source) == (d, "bmo", 3.0, 3.07, "tmx")
    assert ry.surprise_pct == pytest.approx(0.023333)
    assert by["DTOL.TO"].timing == "amc" and by["TRX.TO"].timing == "unknown"
    assert by["AXL.TO"].eps_estimate is None and by["AXL.TO"].eps_actual == 0.03   # "N/A" -> None
    assert tmx.parse_earnings_day(_json("tmx_earnings_empty_day.json"), d, str) == []   # TMX's empty-day error
    with pytest.raises(tmx.Unexpected):
        tmx.parse_earnings_day({"errors": [{"message": "Unknown field"}], "data": None}, d, str)
    with pytest.raises(tmx.Unexpected):
        tmx.parse_earnings_day({"data": {"getEnhancedEarningsForDate": {"rows": []}}}, d, str)


def test_tmx_upcoming_events():
    names = {"RY": "RY.TO", "TECK.B": "TECK.B.TO", "ATD": "ATD.TO", "WELL": "WELL.TO", "DML": "DML.TO"}
    up = tmx.parse_upcoming(_json("tmx_wsh_events.json"), names.get, TODAY)
    assert set(up) == set(names.values())
    assert (up["RY.TO"].date, up["RY.TO"].timing, up["RY.TO"].fiscal_quarter) == (date(2026, 12, 3), "bmo", "Q4 2026")
    assert up["TECK.B.TO"].date == date(2027, 2, 25)                          # qmSymbol names the class B listing
    assert (up["ATD.TO"].date, up["ATD.TO"].timing) == (date(2027, 3, 16), "amc")  # past results (HIS) skipped
    assert up["DML.TO"].timing == "unknown"                                   # "During Mkt": no BMO / AMC
    assert all(e.source == "tmx" for e in up.values())
    assert tmx.parse_upcoming(_json("tmx_wsh_events.json"), names.get, date(2027, 3, 1)).keys() == {"ATD.TO", "WELL.TO",
                                                                                                    "DML.TO"}
    assert tmx.parse_upcoming({"data": {"quote": {"data": {"error": "No matching companies found"}}}}, str, TODAY) == {}


async def test_tmx_fetchers_post_graphql_as_optionlens(monkeypatch):
    sent = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        sent.append((request.method, request.headers["user-agent"], body["operationName"], body["variables"]))
        if body["operationName"] == "WSH":
            return httpx.Response(200, json=_json("tmx_wsh_events.json"))
        return httpx.Response(200, json=_json("tmx_earnings_2024-12-04.json"))

    monkeypatch.setattr(tmx, "_query", _REAL_TMX_QUERY)   # the real one (the suite guards it) over a mock transport
    monkeypatch.setattr(tmx, "UPCOMING_BATCH", 2)
    monkeypatch.setattr(net, "POLITE_RPS", 1000.0)       # (the real gate asks once a second)
    n = net._net()
    await n.client.aclose()
    n.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        evs = await tmx.fetch_earnings_day(date(2024, 12, 4), lambda t: f"{t}.TO")
        got, err = await tmx.fetch_upcoming(["RY.TO", "TECK.B.TO", "ATD.TO"], TODAY)
    finally:
        await net.aclose()
    assert len(evs) == 8 and err is None and set(got) == {"RY.TO", "TECK.B.TO", "ATD.TO"}
    assert {(m, ua) for m, ua, _, _ in sent} == {("POST", net.HONEST_USER_AGENT)}
    assert net.HONEST_USER_AGENT.startswith("OptionLens/") and "Mozilla" not in net.HONEST_USER_AGENT
    assert [v for *_, op, v in sent if op == "WSH"] == [{"symbols": ["RY", "TECK.B"]}, {"symbols": ["ATD"]}]


async def test_tmx_failure_keeps_what_was_answered(monkeypatch):
    calls = []

    async def fake(op, query, variables):
        calls.append(variables["symbols"])
        if len(calls) == 2:
            raise FetchError(tmx.URL, 503)
        p = _json("tmx_wsh_events.json")
        p["data"]["quote"]["data"] = [e for e in p["data"]["quote"]["data"]
                                      if e["qmSymbol"][:-3] in variables["symbols"]]
        return p

    monkeypatch.setattr(tmx, "_query", fake)
    monkeypatch.setattr(tmx, "UPCOMING_BATCH", 1)
    got, err = await tmx.fetch_upcoming(["RY.TO", "TECK.B.TO", "ATD.TO"], TODAY)
    assert set(got) == {"RY.TO"} and err.startswith("503") and len(calls) == 2   # stops at the failure


# ----------------------------------------------------------------------------- Yahoo
def test_yahoo_parse_next_date():
    ev = yahoo.parse_upcoming(_json("yahoo_quote_summary_RY.TO.json"), "RY.TO", TODAY)
    assert (ev.symbol, ev.date, ev.timing, ev.eps_estimate, ev.source) == ("RY.TO", date(2026, 12, 3), "bmo", 4.15,
                                                                           "yahoo")
    assert yahoo.parse_upcoming(_json("yahoo_quote_summary_RY.TO.json"), "RY.TO", date(2026, 12, 4)) is None

    def summary(*stamps, estimate=False):
        return {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {
            "earningsDate": [{"raw": int(s.timestamp())} for s in stamps], "isEarningsDateEstimate": estimate}}}]}}

    utc = timezone.utc
    after = yahoo.parse_upcoming(summary(datetime(2026, 12, 3, 21, 5, tzinfo=utc)), "X.TO", TODAY)
    assert (after.date, after.timing) == (date(2026, 12, 3), "amc")           # 16:05 Toronto
    midnight = yahoo.parse_upcoming(summary(datetime(2026, 12, 3, tzinfo=utc)), "X.TO", TODAY)
    assert (midnight.date, midnight.timing) == (date(2026, 12, 3), "unknown")   # a date without a time
    window = yahoo.parse_upcoming(summary(datetime(2027, 2, 2, 12, tzinfo=utc), datetime(2027, 2, 6, 12, tzinfo=utc)),
                                  "X.TO", TODAY)
    assert (window.date, window.timing) == (date(2027, 2, 2), "unknown")     # an estimated window: its first day
    assert yahoo.parse_upcoming({"quoteSummary": {"result": None, "error": {"code": "Not Found"}}}, "X.TO", TODAY) is None


async def test_yahoo_crumb_handshake_and_fail_fast(monkeypatch):
    seen = []
    state = {"crumb_status": 200}

    def handler(request: httpx.Request):
        seen.append((request.url.host, request.url.path, request.headers["user-agent"]))
        if request.url.host == "fc.yahoo.com":
            return httpx.Response(404, headers={"set-cookie": "A3=d=abc; Domain=.yahoo.com; Path=/"}, text="<html/>")
        if request.url.path.endswith("getcrumb"):
            assert "A3=d=abc" in request.headers.get("cookie", "")
            if state["crumb_status"] == 429:
                return httpx.Response(429, headers={"content-type": "text/plain"}, text="Too Many Requests")
            return httpx.Response(200, text="Xy1zCrumb")
        assert request.url.params["crumb"] == "Xy1zCrumb"
        if "ZZZZ" in request.url.path:
            return httpx.Response(404, json={"quoteSummary": {"result": None, "error": {"code": "Not Found"}}})
        return httpx.Response(200, json=_json("yahoo_quote_summary_RY.TO.json"))

    monkeypatch.setattr(yahoo, "_summary", _REAL_YAHOO_SUMMARY)   # the real one over a mock transport
    monkeypatch.setattr(net, "get", _REAL_NET_GET)
    monkeypatch.setattr(net, "_save_blocks", lambda: None)
    monkeypatch.setattr(net, "POLITE_RPS", 1000.0)
    n = net._net()
    await n.client.aclose()
    n.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        got, err = await yahoo.fetch_upcoming(["RY.TO", "ZZZZ.TO"], TODAY)
        assert err is None and set(got) == {"RY.TO"} and got["RY.TO"].date == date(2026, 12, 3)
        assert [p for _, p, _ in seen].count("/v1/test/getcrumb") == 1        # one handshake per client
        assert {ua for *_, ua in seen} == {net.HONEST_USER_AGENT}
        assert any("/quoteSummary/RY.TO" in p for _, p, _ in seen)
        yahoo._crumbs.clear()
        state["crumb_status"] = 429
        before = len(seen)
        got, err = await yahoo.fetch_upcoming(["RY.TO", "TD.TO"], TODAY)
        assert got == {} and err.startswith("429") and "Too Many Requests" in err
        assert len(seen) - before == 2                                        # cookie + one crumb try: no retries
    finally:
        yahoo._crumbs.clear()
        await net.aclose()


# ----------------------------------------------------------------------------- selection
def _ev(sym, d, source, timing="bmo"):
    return EarningsEvent(sym, d, timing, None, None, None, None, None, source)


def _fetcher(answers, err=None, calls=None):
    async def fetch(symbols, today):
        if calls is not None:
            calls.append(list(symbols))
        return {s: ev for s, ev in answers.items() if s in symbols}, err
    return fetch


async def test_select_tmx_answers_and_yahoo_is_not_asked():
    yahoo_calls = []
    sel = await ca_earnings.select_upcoming(["RY.TO", "TD.TO"], TODAY, fetchers={
        "tmx": _fetcher({"RY.TO": _ev("RY.TO", date(2026, 12, 3), "tmx"), "TD.TO": _ev("TD.TO", date(2026, 12, 3), "tmx")}),
        "yahoo": _fetcher({}, calls=yahoo_calls)})
    assert {s: e.source for s, e in sel.events.items()} == {"RY.TO": "tmx", "TD.TO": "tmx"}
    assert sel.health == {"tmx": None} and yahoo_calls == [] and sel.unanswered == ()
    assert sel.counts() == {"tmx": 2, "yahoo": 0, "none": 0}


async def test_select_falls_back_to_yahoo_when_tmx_fails():
    yahoo_calls = []
    sel = await ca_earnings.select_upcoming(["RY.TO", "TD.TO", "BNS.TO"], TODAY, fetchers={
        "tmx": _fetcher({"RY.TO": _ev("RY.TO", date(2026, 12, 3), "tmx")}, err="503 app-money.tmx.com"),
        "yahoo": _fetcher({"TD.TO": _ev("TD.TO", date(2026, 12, 4), "yahoo")}, calls=yahoo_calls)})
    assert {s: e.source for s, e in sel.events.items()} == {"RY.TO": "tmx", "TD.TO": "yahoo"}
    assert yahoo_calls == [["TD.TO", "BNS.TO"]]                       # only what TMX left
    assert sel.health == {"tmx": "503 app-money.tmx.com", "yahoo": None} and sel.unanswered == ("BNS.TO",)
    assert sel.why_none() == "TMX Money was unavailable (503 app-money.tmx.com); Yahoo Finance has no upcoming date"


async def test_select_both_fail_says_why():
    sel = await ca_earnings.select_upcoming(["RY.TO"], TODAY, fetchers={
        "tmx": _fetcher({}, err="503 app-money.tmx.com"), "yahoo": _fetcher({}, err="429 query2.finance.yahoo.com")})
    assert sel.events == {} and sel.unanswered == ("RY.TO",)
    assert sel.why_none() == ("TMX Money and Yahoo Finance were unavailable (TMX Money: 503 app-money.tmx.com; "
                              "Yahoo Finance: 429 query2.finance.yahoo.com)")
    tmx_calls = []
    sel = await ca_earnings.select_upcoming(["RY.TO"], TODAY, down={"tmx": "calendar failed"}, fetchers={
        "tmx": _fetcher({}, calls=tmx_calls), "yahoo": _fetcher({}, err="429")})
    assert tmx_calls == [] and sel.health == {"tmx": "calendar failed", "yahoo": "429"}


# ----------------------------------------------------------------------------- jobs
CA_SYMBOLS = [{"symbol": "RY.TO", "kind": "stock", "market_cap": 2.6e11}, {"symbol": "TECK.B.TO", "kind": "stock"},
              {"symbol": "XYZ.VN", "kind": "stock"}, {"symbol": "XIU.TO", "kind": "etf"},
              {"symbol": "AAPL", "kind": "stock"}]


@pytest.fixture
def ca_db(tmpdb, monkeypatch):
    symbols_repo.upsert_symbols(CA_SYMBOLS)
    monkeypatch.setattr(calendars, "now_ny", lambda: ny(2026, 11, 26, 6, 0))
    earn_repo.upsert_earnings([_ev("AAPL", date(2026, 11, 27), "nasdaq"), _ev("RY", date(2026, 12, 3), "nasdaq")])
    return tmpdb


def _fake_tmx_calendar(monkeypatch, fail_on: date | None = None):
    asked = []

    async def day(d, to_symbol):
        asked.append(d)
        if d == fail_on:
            raise FetchError(tmx.URL, 503)
        rows = {date(2026, 12, 3): [("RY", "bmo", 4.15), ("CM", "bmo", 2.47)],
                date(2026, 11, 20): [("XYZ", "unknown", None)]}.get(d, [])
        return [EarningsEvent(to_symbol(t), d, tm, None, None, est, None, None, "tmx") for t, tm, est in rows]

    monkeypatch.setattr(tmx, "fetch_earnings_day", day)
    return asked


async def test_earnings_ca_job_stores_canadian_events_with_their_source(ca_db, monkeypatch):
    asked = _fake_tmx_calendar(monkeypatch)
    upcoming_calls, yahoo_calls = [], []

    async def wsh(symbols, today):
        upcoming_calls.append(list(symbols))
        return {"TECK.B.TO": _ev("TECK.B.TO", date(2027, 2, 25), "tmx")}, None

    async def yh(symbols, today):
        yahoo_calls.append(list(symbols))
        return {}, None

    monkeypatch.setattr(tmx, "fetch_upcoming", wsh)
    monkeypatch.setattr(yahoo, "fetch_upcoming", yh)
    ok, detail = await scheduler.run_job("earnings_ca")
    assert ok, detail
    assert all(d.weekday() < 5 for d in asked) and asked[0] == date(2026, 11, 16) and asked[-1] == date(2027, 2, 24)
    assert upcoming_calls == [["RY.TO", "TECK.B.TO", "XYZ.VN"]]              # stocks only (no ETF, no US listing)
    assert yahoo_calls == [["XYZ.VN"]]                                       # TMX had no date for it
    ry = earn_repo.next_events(TODAY)["RY.TO"]
    assert (ry.date, ry.timing, ry.eps_estimate, ry.source) == (date(2026, 12, 3), "bmo", 4.15, "tmx")  # calendar date
    assert earn_repo.next_events(TODAY)["TECK.B.TO"].date == date(2027, 2, 25)
    assert earn_repo.for_symbol("CM.TO")[0].source == "tmx"                  # unknown tickers: the TSX listing
    assert earn_repo.for_symbol("XYZ.VN")[0].date == date(2026, 11, 20)     # a known Venture listing keeps .VN
    # US listings untouched (Nasdaq rows; RY is the NYSE listing, not RY.TO)
    assert [(e.symbol, e.source) for e in earn_repo.between(date(2026, 11, 27), date(2026, 12, 3), ["AAPL", "RY"])] == \
        [("AAPL", "nasdaq"), ("RY", "nasdaq")]
    assert detail.endswith('{"tmx": "ok", "yahoo": "ok"}') and "tmx 2, yahoo 0, none 1" in detail
    assert earn_repo.lookup("XYZ.VN")["detail"] == "TMX Money has no upcoming date; Yahoo Finance has no upcoming date"
    assert earn_repo.lookup("RY.TO")["source"] == "tmx"
    # the same day again: answered listings are not looked up again
    upcoming_calls.clear()
    ok, detail = await scheduler.run_job("earnings_ca")
    assert ok and upcoming_calls == [["XYZ.VN"]] and "(2 looked up earlier today)" in detail
    health = calendars.ca_source_health()
    assert health["tmx"]["state"] == "ok" and health["tmx"]["last_success_at"] is not None
    st = {s["name"]: s for s in system.ca_earnings_sources()}
    assert st["tmx"]["ok"] is True and st["yahoo"]["ok"] is True


async def test_earnings_ca_yahoo_fallback_and_both_down(ca_db, monkeypatch):
    _fake_tmx_calendar(monkeypatch, fail_on=date(2026, 11, 16))       # TMX's first request fails: TMX is down
    wsh_calls = []

    async def wsh(symbols, today):
        wsh_calls.append(symbols)
        return {}, None

    async def yh(symbols, today):
        return {"RY.TO": _ev("RY.TO", date(2026, 12, 3), "yahoo", "unknown")}, "429 query2.finance.yahoo.com"

    monkeypatch.setattr(tmx, "fetch_upcoming", wsh)
    monkeypatch.setattr(yahoo, "fetch_upcoming", yh)
    ok, detail = await scheduler.run_job("earnings_ca")
    assert ok and wsh_calls == []                                          # TMX unavailable for the rest of the run
    assert earn_repo.next_events(TODAY)["RY.TO"].source == "yahoo"
    assert '"yahoo": "429 query2.finance.yahoo.com"' in detail and '"tmx": "503' in detail
    st = {s["name"]: s for s in system.ca_earnings_sources()}
    assert st["tmx"]["ok"] is False and st["yahoo"]["ok"] is False and "503" in st["tmx"]["detail"]
    lk = earn_repo.lookup("TECK.B.TO")
    assert lk["source"] is None and lk["detail"].startswith("TMX Money and Yahoo Finance were unavailable (TMX Money: 503")
    ctx.invalidate()
    msg = symbol_earnings.unavailable("TECK.B.TO")
    assert msg.startswith("Earnings dates for TECK.B.TO are not available: TMX Money and Yahoo Finance were unavailable")
    assert symbol_earnings.unavailable("RY.TO") is None and symbol_earnings.unavailable("AAPL") is None

    async def nothing(symbols, today):
        return {}, "429 query2.finance.yahoo.com"

    monkeypatch.setattr(yahoo, "fetch_upcoming", nothing)
    ok, detail = await scheduler.run_job("earnings_ca")
    assert not ok and detail.startswith("RuntimeError: TMX calendar: 0 events") and "\"yahoo\": \"429" in detail


async def test_earnings_ca_without_canadian_listings_asks_nothing(tmpdb, monkeypatch):
    asked = _fake_tmx_calendar(monkeypatch)
    symbols_repo.upsert_symbols([{"symbol": "AAPL", "kind": "stock"}])
    assert await calendars.earnings_ca() == calendars.NO_CA and asked == []
    assert await calendars.earnings_backfill_ca() == calendars.NO_CA and asked == []


async def test_earnings_backfill_ca_is_resumable(ca_db, monkeypatch):
    monkeypatch.setattr(calendars, "BACKFILL_YEARS", 0.1)
    fetched, fail = [], {"at": 5}

    async def day(d, to_symbol):
        if len(fetched) == fail["at"]:
            raise FetchError(tmx.URL, 503)
        fetched.append(d)
        return [EarningsEvent(to_symbol("RY"), d, "bmo", None, 1.0, 1.0, 0.0, None, "tmx")]

    monkeypatch.setattr(tmx, "fetch_earnings_day", day)
    ok, _ = await scheduler.run_job("earnings_backfill_ca")
    assert not ok and len(fetched) == 5
    assert 0 < calendars.earnings_backfill_progress(TODAY, calendars.CA_BACKFILL_JOB) < 1
    assert calendars.earnings_backfill_progress(TODAY) == 0.0               # Nasdaq's backfill is its own
    fail["at"] = -1
    ok, _ = await scheduler.run_job("earnings_backfill_ca")
    assert ok and calendars.earnings_backfill_progress(TODAY, calendars.CA_BACKFILL_JOB) == 1.0
    assert len(fetched) == len(set(fetched)) and {e.symbol for e in earn_repo.between(fetched[-1], TODAY)} == {"RY.TO"}


# ----------------------------------------------------------------------------- pages / calculator
def test_next_missing_texts(ca_db):
    ctx.invalidate()
    assert symbol_earnings.next_missing("AAPL") is None
    assert "in the Nasdaq calendar" in symbol_earnings.next_missing("MSFT")
    assert symbol_earnings.next_missing("TECK.B.TO") == (
        "No upcoming earnings date for TECK.B.TO: not looked up yet (Canadian listings' dates come from TMX Money, "
        "else Yahoo Finance (unofficial sources), looked up daily).")


def test_canadian_reaction_day_on_the_tsx_calendar():
    """An after-close report on Wed 2026-11-25 trades on Thursday the 26th in Toronto (US Thanksgiving: NYSE closed)."""
    assert ctx.expected_reaction_day(_ev("RY.TO", date(2026, 11, 25), "tmx", "amc")) == date(2026, 11, 26)
    assert ctx.expected_reaction_day(_ev("RY", date(2026, 11, 25), "nasdaq", "amc")) == date(2026, 11, 27)
