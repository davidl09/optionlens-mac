"""Questrade JSON -> models (sources/questrade/mapping.py) on the documentation's sample responses
(tests/fixtures/questrade/doc_*.json: transcribed from questrade.com/api/documentation with typographic quotes made
ASCII and the sample's `"volume": 0.` read as 0; field names and values unchanged), and QuestradeSource.fetch_chain
end to end against the in-memory Questrade (tests/questrade_fake.py)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from optionlens.analytics.calendar import NY
from optionlens.net import FetchError
from optionlens.sources.chain_source import NotListed
from optionlens.sources.questrade import auth, mapping, source as source_mod
from optionlens.sources.questrade.source import QuestradeSource

from .questrade_fake import FakeQuestrade, option_id

FIX = Path(__file__).parent / "fixtures" / "questrade"
NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)


def doc(name: str) -> dict:
    return json.loads((FIX / f"doc_{name}.json").read_text())


# ----------------------------------------------------------------------------- documented samples
def test_token_sample():
    t = doc("token")
    assert auth.api_base(t["api_server"]) == "https://api01.iq.questrade.com" and t["expires_in"] == 1800


def test_symbol_search_sample():
    s = doc("symbols_search")
    assert mapping.pick_symbol_id(s, "BMO") == 9292                     # the USD listing
    assert mapping.pick_symbol_id(s, "BMO.PRJ") is None                 # CAD (TSX) listings are not ours
    assert mapping.pick_symbol_id({"symbols": []}, "BMO") is None       # the field table's spelling
    idx = {"symbols": [{"symbol": "SPX.IN", "symbolId": 7, "securityType": "Index", "currency": "USD"},
                       {"symbol": "SPXL", "symbolId": 8, "securityType": "Stock", "currency": "USD"}]}
    assert mapping.pick_symbol_id(idx, "SPX") == 7 and mapping.pick_symbol_id(idx, "SPXL") == 8


def test_symbol_info_sample():
    i = mapping.symbol_info(doc("symbols_id"))
    assert i.has_options is True and i.prev_close == 102.5


def test_option_chain_structure_sample():
    expiries, listed = mapping.listed_contracts(doc("symbols_options"), "BMO")
    assert expiries == {date(2015, 1, 17): "2015-01-17T00:00:00.000000-05:00"}
    assert listed[6101993] == mapping.Listed(6101993, date(2015, 1, 17), 60.0, "C")
    assert listed[6102011] == mapping.Listed(6102011, date(2015, 1, 17), 64.0, "P")
    assert len(listed) == 6


def test_equity_quote_sample():
    q = mapping.parse_quote(doc("markets_quotes")["quotes"][0], "THI", 82.0, NOW, final=False)
    assert (q.price, q.bid, q.ask, q.open, q.high, q.low, q.volume) == (83.66, 83.65, 83.67, 83.66, 83.86, 83.66, 80483500)
    assert q.change == pytest.approx(1.66) and q.change_pct == pytest.approx(1.66 / 82.0)
    assert q.delayed is False and q.ts == NOW and q.iv30 is None


def test_option_quote_sample():
    quotes = mapping.option_quotes(doc("markets_quotes_options"))
    listed = mapping.Listed(7413503, date(2017, 1, 20), 70.0, "C")
    c = mapping.contract("MSFT", listed, quotes[7413503])
    assert c.osi == "MSFT170120C00070000" and (c.bid, c.ask, c.last) == (4.90, 4.95, 4.93)
    assert c.iv == pytest.approx(0.52374257)                            # percent -> fraction
    assert (c.delta, c.gamma, c.theta, c.vega, c.rho) == (0.06985, 0.01038, -0.001406, 0.074554, 0.04153)
    assert c.oi == 2292 and c.volume == 0                               # reported values kept
    assert c.last_ts == datetime(2015, 8, 17, tzinfo=NY).astimezone(timezone.utc)
    assert c.iv_source is None and c.theo is None                       # labelled by the pipeline


# ----------------------------------------------------------------------------- hand-made edge cases
def test_unknown_is_null_never_zero():
    listed = mapping.Listed(1, date(2026, 12, 18), 100.0, "P")
    none = mapping.contract("XYZ", listed, None)                        # listed, not quoted
    assert (none.bid, none.ask, none.last, none.volume, none.oi, none.iv, none.delta) == (None,) * 7
    c = mapping.contract("XYZ", listed, {"bidPrice": 0, "askPrice": 0, "lastTradePrice": 0, "volatility": 0,
                                          "delta": 0, "gamma": 0, "volume": None, "openInterest": None})
    assert (c.bid, c.ask, c.last, c.iv, c.delta, c.gamma, c.volume, c.oi) == (None,) * 8
    c = mapping.contract("XYZ", listed, {"bidPrice": 0, "askPrice": 0.05})
    assert (c.bid, c.ask) == (0.0, 0.05)                                # a 0 bid counts against a real ask


def test_roots_multipliers_and_pm_index_roots():
    def chain(*roots):
        return {"optionChain": [{"expiryDate": "2026-12-18T00:00:00.000000-05:00", "chainPerRoot": [
            {"optionRoot": r, "multiplier": m, "chainPerStrikePrice": [
                {"strikePrice": 6000, "callSymbolId": i, "putSymbolId": i + 1}]} for r, m, i in roots]}]}
    _, listed = mapping.listed_contracts(chain(("SPX", 100, 10), ("SPXW", 100, 20)), "SPX")
    assert set(listed) == {20, 21}                                      # PM root kept, one OSI per contract
    assert mapping.contract("SPX", listed[20], None).osi == "SPXW261218C06000000"
    _, listed = mapping.listed_contracts(chain(("SPX", 100, 10)), "SPX")
    assert set(listed) == {10, 11}
    _, listed = mapping.listed_contracts(chain(("AAPL", 100, 1), ("AAPL1", 100, 3), ("AAPL", 10, 5)), "AAPL")
    assert set(listed) == {1, 2}                                        # adjusted root and mini contracts dropped
    _, listed = mapping.listed_contracts({"options": [{"expiryDate": "2026-12-18T00:00:00.000000-05:00",
                                                        "chainPerRoot": [{"root": "BRK.B", "chainPerStrikePrice": [
                                                            {"strikePrice": 500, "callSymbolId": 9}]}]}]}, "BRK.B")
    assert mapping.contract("BRK.B", listed[9], None).osi == "BRKB261218C00500000"


def test_final_quote_uses_the_regular_hours_last_and_delay_flags():
    q = {"lastTradePriceTrHrs": 100.0, "lastTradePrice": 100.4, "delay": True}
    assert mapping.parse_quote(q, "X", None, NOW, final=True).price == 100.0
    live = mapping.parse_quote(q, "X", None, NOW, final=False)
    assert live.price == 100.4 and live.delayed is True and live.change is None
    assert mapping.parse_quote({"bidPrice": 9.0, "askPrice": 11.0}, "X", None, NOW, False).price == 10.0
    assert mapping.is_delayed({"delay": 15}) and not mapping.is_delayed({"delay": 0})


# ----------------------------------------------------------------------------- QuestradeSource.fetch_chain
@pytest.fixture
def qt(tmp_path, monkeypatch):
    fake = FakeQuestrade()
    src = QuestradeSource(tmp_path, expiries=2, moneyness=0.25, http=fake.http)
    monkeypatch.setattr(source_mod, "now_ny", lambda: datetime(2026, 11, 19, 10, 0, tzinfo=NY))
    return fake, src


async def test_fetch_chain_end_to_end(qt):
    fake, src = qt
    assert src.unavailable() == "not connected"
    await src.tokens.connect("pasted-token")
    snap = await src.fetch_chain("XYZ")
    assert snap.quote.price == 100.2 and snap.quote.prev_close == 98.0 and snap.delayed is False
    # first 2 expiries on/after today (11-13 expired), strikes within ±25% of 100.2, XYZ1 dropped
    assert sorted({c.expiry for c in snap.contracts}) == [date(2026, 11, 20), date(2026, 11, 27)]
    assert sorted({c.strike for c in snap.contracts}) == [80.0, 90.0, 100.0, 110.0, 120.0]
    assert len(snap.contracts) == 2 * 5 * 2
    by = {c.osi: c for c in snap.contracts}
    assert by["XYZ261120P00100000"].bid is None and by["XYZ261120P00100000"].ask is None    # listed, unquoted
    assert by["XYZ261120C00090000"].iv is None and by["XYZ261120C00090000"].delta is None   # no volatility
    assert by["XYZ261127C00110000"].iv == pytest.approx(0.315) and by["XYZ261127C00110000"].bid == 1.0
    body = json.loads(next(r for r in fake.requests if r.url.path == "/v1/markets/quotes/options").content)
    assert [f["expiryDate"][:10] for f in body["filters"]] == ["2026-11-20", "2026-11-27"]
    assert [(f["underlyingId"], round(f["minstrikePrice"], 2), round(f["maxstrikePrice"], 2))
            for f in body["filters"]] == [(1000, 75.15, 125.25)] * 2
    assert src.status()["delayed"] is False and src.status()["state"] == "connected"
    # symbol id cached on disk, symbol info per day: a second chain costs 3 calls
    n = len(fake.requests)
    await src.fetch_chain("XYZ")
    assert len(fake.requests) - n == 3 and fake.path_count("/v1/symbols/search") == 1
    assert json.loads((src.ids.path).read_text())["XYZ"]["id"] == 1000


async def test_fetch_chain_delayed_not_listed_and_no_options(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    fake.delay = 15
    assert (await src.fetch_chain("XYZ")).delayed is True and src.status()["delayed"] is True
    with pytest.raises(NotListed):
        await src.fetch_chain("NOPE")
    with pytest.raises(NotListed):                                      # negative answer cached
        await src.fetch_chain("NOPE")
    assert sum(1 for r in fake.requests if r.url.params.get("prefix") == "NOPE") == 1
    src._info.clear()
    import httpx
    fake.queue["/v1/symbols/1000"] = [httpx.Response(200, json={"symbols": [{"symbolId": 1000, "hasOptions": False}]})]
    empty = await src.fetch_chain("XYZ")
    assert empty.contracts == () and empty.quote.price == 100.2       # no options: empty chain
    fake.queue["/v1/markets/quotes"] = [httpx.Response(200, json={"quotes": []})]
    with pytest.raises(FetchError, match="no quote"):
        await src.fetch_chain("XYZ")
    assert option_id("2026-11-20", 100, "P") in fake.unquoted


def test_option_filters_send_whole_cent_bounds():
    """Questrade rejects float-noise strikes (37.964999999999996) with 400 code 1013; bounds go out in cents,
    widened outward so the band never shrinks."""
    import json
    from optionlens.sources.questrade.mapping import option_filters

    f = option_filters(40445, ["2026-12-18T00:00:00.000000-05:00"], 50.62 * 0.75, 50.62 * 1.25)
    assert f[0]["minstrikePrice"] == 37.96 and f[0]["maxstrikePrice"] == 63.28
    body = json.dumps({"filters": f})
    assert "37.96," in body and "999999" not in body


# ----------------------------------------------------------------------------- reference data, VWAP, halts
def test_symbol_info_sample_reference_data():
    """GET v1/symbols/:id sample (AAPL): every reference field by its exact sample name; `dividend` is one payment
    (4 × 0.47 / prevDayClosePrice 102.5 = 1.83 % ≈ the sample's yield 1.84), `yield` percent -> fraction."""
    i = mapping.symbol_info(doc("symbols_id"))
    assert (i.pe, i.eps, i.market_cap, i.shares_outstanding) == (16.54, 6.2, 613756367500, 5987867000)
    assert (i.high_52w, i.low_52w, i.avg_volume_20d, i.avg_volume_3m) == (102.9, 63.89, 12860370, 43769680)
    assert (i.sector, i.industry_group, i.industry_subgroup) == ("BasicMaterials", "Steel", "Steel")
    assert i.dividend == 0.47 and i.dividend_yield == pytest.approx(0.0184)
    assert 4 * i.dividend / i.prev_close == pytest.approx(i.dividend_yield, abs=0.0002)    # per payment, annualized
    assert (i.ex_date, i.pay_date) == (date(2014, 8, 7), date(2014, 8, 14))
    f = mapping.fundamentals(i, "AAPL", date(2026, 11, 19))
    assert (f.source, f.pe, f.market_cap, f.prev_close, f.dividend_yield) == ("questrade", 16.54, 613756367500, 102.5,
                                                                              pytest.approx(0.0184))
    d = mapping.current_dividend(i, "AAPL")
    assert (d.ex_date, d.amount, d.pay_date, d.source, d.kind) == (date(2014, 8, 7), 0.47, date(2014, 8, 14),
                                                                   "questrade", "cash")
    bare = mapping.symbol_info({"symbols": [{"hasOptions": False, "dividend": 0, "yield": None, "exDate": None}]})
    assert mapping.current_dividend(bare, "X") is None and bare.dividend_yield is None and bare.pe is None


def test_equity_quote_sample_halt_vwap_and_extended_hours():
    q = doc("markets_quotes")["quotes"][0]
    p = mapping.parse_quote(q, "THI", 82.0, NOW, final=False)
    assert p.halted is False and p.vwap is None                    # VWAP is not in the documented sample
    assert mapping.parse_quote({**q, "VWAP": 83.71, "isHalted": True}, "THI", 82.0, NOW, False).vwap == 83.71
    live = mapping.live_quote({**q, "VWAP": 83.71, "isHalted": True}, "THI", 82.0, NOW)
    assert live["vwap"] == 83.71 and live["halted"] is True
    x = mapping.extended_quote({**q, "lastTradePrice": 84.5}, "THI", NOW)
    assert (x.price, x.regular_price, x.volume, x.halted, x.delayed) == (84.5, 83.66, 80483500, False, False)
    assert x.last_trade_at == datetime(2014, 10, 24, 20, 6, 40, 131000, tzinfo=x.last_trade_at.tzinfo)


def test_candles_keep_their_vwap_when_sent():
    c = {"start": "2026-11-19T09:30:00.000000-05:00", "end": "2026-11-19T09:31:00.000000-05:00", "low": 1.0,
         "high": 2.0, "open": 1.5, "close": 1.6, "volume": 10}
    assert mapping.candles({"candles": [c]})[0].vwap is None
    assert mapping.candles({"candles": [{**c, "VWAP": 1.55}]})[0].vwap == 1.55


async def test_reference_data_is_cached_with_the_chains_and_extended_quotes_use_cached_ids(qt):
    fake, src = qt
    await src.tokens.connect("pasted-token")
    fake.info_extra = {k: v for k, v in doc("symbols_id")["symbols"][0].items() if k not in ("symbol", "symbolId")}
    await src.fetch_chain("XYZ")
    today = date(2026, 11, 19)
    ref = src.cached_reference(today)
    f, d = ref["XYZ"]
    assert (f.symbol, f.date, f.eps, f.industry_subgroup) == ("XYZ", today, 6.2, "Steel") and d.amount == 0.47
    assert src.cached_reference(date(2026, 11, 20)) == {}
    n = len(fake.requests)
    fake.quote_extra = {"lastTradePrice": 101.5, "isHalted": True, "lastTradeTime": "2026-11-19T17:00:00.000000-05:00"}
    got = await src.fetch_extended(["XYZ", "NOPE", "SPY"])                       # NOPE / SPY: no cached id
    assert [(x.symbol, x.price, x.regular_price, x.halted) for x in got] == [("XYZ", 101.5, 100.0, True)]
    assert len(fake.requests) - n == 1 and fake.path_count("/v1/symbols/search") == 1       # no searches
