"""Parsers of CBOE / Nasdaq / Treasury payloads against real captured fixtures (offline)."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from optionlens.analytics.calendar import NY
from optionlens.models import normalize_symbol
from optionlens.sources import cboe, nasdaq, treasury

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


# ----------------------------------------------------------------------------- CBOE
@pytest.mark.parametrize("osi, expected", [
    ("AAPL261120C00100000", ("AAPL", date(2026, 11, 20), "C", 100.0)),
    ("SPXW261120P05000000", ("SPXW", date(2026, 11, 20), "P", 5000.0)),
    ("SPX261218C07325000", ("SPX", date(2026, 12, 18), "C", 7325.0)),
    ("XSP261218C00612500", ("XSP", date(2026, 12, 18), "C", 612.5)),
    ("AAPL1261120P00002500", ("AAPL1", date(2026, 11, 20), "P", 2.5)),
    ("F270115C00012130", ("F", date(2027, 1, 15), "C", 12.13)),
    ("BRK.B261120C00500000", ("BRK.B", date(2026, 11, 20), "C", 500.0)),
])
def test_parse_osi(osi, expected):
    assert cboe.parse_osi(osi) == expected


@pytest.mark.parametrize("bad", ["", "AAPL", "AAPL261120X00100000", "AAPL26112C00100000", "aapl261120C00100000"])
def test_parse_osi_rejects_malformed(bad):
    with pytest.raises(ValueError):
        cboe.parse_osi(bad)


def test_format_osi_builds_standard_codes():
    from optionlens.sources.osi import format_osi, parse_osi
    assert format_osi("AAPL", date(2026, 11, 20), "C", 100.0) == "AAPL261120C00100000"
    assert format_osi("BRK.B", date(2026, 11, 20), "P", 512.5) == "BRKB261120P00512500"
    assert format_osi("SPX", date(2026, 12, 18), "C", 7325.0) == "SPXW261218C07325000"     # PM root
    assert format_osi("F", date(2027, 1, 15), "C", 12.13) == "F270115C00012130"
    assert parse_osi(format_osi("XSP", date(2026, 12, 18), "P", 612.5)) == ("XSP", date(2026, 12, 18), "P", 612.5)


def test_normalize_symbol():
    assert normalize_symbol(" ^spx ") == "SPX"
    assert normalize_symbol("_NDX") == "NDX"
    assert normalize_symbol("brk/b") == "BRK.B"


def test_parse_chain_aapl():
    snap = cboe.parse_chain(load("cboe_chain_AAPL.json"), "AAPL")
    q = snap.quote
    assert len(snap.contracts) == 3386
    assert q.symbol == "AAPL" and q.price == pytest.approx(348.515)
    assert q.iv30 == pytest.approx(0.25007)            # vol points -> fraction
    assert q.change_pct == pytest.approx(-0.011165)    # percent -> fraction
    assert q.prev_close == pytest.approx(352.45) and q.volume == 18174222
    assert q.ts == datetime(2026, 11, 19, 13, 8, 10, tzinfo=NY)
    c = snap.contracts[0]
    assert (c.osi, c.symbol, c.expiry, c.strike, c.right) == ("AAPL261120C00100000", "AAPL", date(2026, 11, 20), 100.0, "C")
    assert c.iv is None                                 # feed 0 -> None
    assert (c.bid, c.ask, c.last, c.oi, c.volume) == (247.7, 250.6, 249.51, 6, 2)
    assert c.delta == 1.0 and c.rho == pytest.approx(0.0082)
    last = snap.contracts[-1]
    assert last.right == "P" and last.last is None and last.iv == pytest.approx(0.4106)
    assert all(isinstance(x.volume, int) and isinstance(x.oi, int) for x in snap.contracts)
    assert snap.fetched_at.tzinfo is not None
    assert c.mid == pytest.approx((247.7 + 250.6) / 2)


def test_parse_quote_vix_index():
    q = cboe.parse_quote(load("cboe_quote_VIX.json"), "VIX")
    assert q.symbol == "VIX" and q.price == pytest.approx(27.82)
    assert q.bid is None and q.ask is None and q.volume == 0
    assert q.change_pct == pytest.approx(0.132736)


def test_parse_history_csv_ohlc_and_close_only():
    bars = cboe.parse_history_csv((FIX / "cboe_VIX_History.csv").read_text(), "VIX")
    assert bars[0].date == date(1990, 1, 2) and bars[0].close == pytest.approx(17.24)
    assert bars[0].open == pytest.approx(17.24) and bars[0].volume is None
    assert all(a.date < b.date for a, b in zip(bars, bars[1:]))
    spx = cboe.parse_history_csv("DATE,SPX\n01/02/1975,70.230000\n01/03/1975,70.710000\n", "SPX")
    assert [(b.date, b.open, b.close) for b in spx] == [(date(1975, 1, 2), None, 70.23), (date(1975, 1, 3), None, 70.71)]
    with pytest.raises(ValueError):
        cboe.parse_history_csv("<html>nope</html>", "X")


# ----------------------------------------------------------------------------- Nasdaq scalars
@pytest.mark.parametrize("raw, expected", [
    ("$352.45", 352.45), ("34,209,930", 34209930.0), ("N/A", None), ("--", None), ("", None), (None, None),
    ("($0.08)", -0.08), ("-3.44", -3.44), ("+0.98", 0.98), ("2.926%", 2.926), (12, 12.0), (0.05, 0.05),
    ("21,616,917.682800", 21616917.6828), ("$5,121,867,500,000", 5121867500000.0), ("$", None),
])
def test_num(raw, expected):
    assert nasdaq.num(raw) == (pytest.approx(expected) if expected is not None else None)


def test_day_formats():
    assert nasdaq.day("11/18/2026") == date(2026, 11, 18)
    assert nasdaq.day("7/3/2026") == date(2026, 7, 3)
    assert nasdaq.day("Nov 12, 2026") == date(2026, 11, 12)
    assert nasdaq.day("N/A") is None and nasdaq.day("") is None and nasdaq.day(None) is None


# ----------------------------------------------------------------------------- Nasdaq parsers
def test_parse_history():
    bars = nasdaq.parse_history(load("nasdaq_history_AAPL.json"), "AAPL")
    assert len(bars) == 2513
    assert bars[0].date == date(2016, 11, 18) and bars[0].close == pytest.approx(27.515)
    last = bars[-1]
    assert (last.date, last.close, last.volume, last.open, last.low) == (
        date(2026, 11, 18), 352.45, 34209930, 351.94, 351.4706)


def test_parse_history_robust_to_missing_values_and_null_data():
    p = {"data": {"tradesTable": {"rows": [
        {"date": "11/18/2026", "close": "730.20", "volume": "N/A", "open": "--", "high": "", "low": "729"},
        {"date": "11/17/2026", "close": "N/A", "volume": "1", "open": "1", "high": "1", "low": "1"},
    ]}}}
    bars = nasdaq.parse_history(p, "SPY")
    assert len(bars) == 1 and bars[0].volume is None and bars[0].open is None and bars[0].low == 729
    assert nasdaq.parse_history({"data": None, "status": {"rCode": 400}}, "ZZZ") == []


def test_parse_earnings():
    ev = {e.symbol: e for e in nasdaq.parse_earnings(load("nasdaq_earnings_2023-05-04.json"), date(2023, 5, 4))}
    aapl = ev["AAPL"]
    assert aapl.date == date(2023, 5, 4) and aapl.timing == "unknown"
    assert (aapl.eps_actual, aapl.eps_estimate, aapl.num_estimates) == (1.52, 1.44, 12)
    assert aapl.surprise_pct == pytest.approx(0.0556) and aapl.fiscal_quarter == "Mar/2023"
    assert ev["SHOP"].eps_actual == pytest.approx(-0.08) and ev["SHOP"].eps_estimate == pytest.approx(-0.16)
    assert ev["NVO"].surprise_pct is None


def test_parse_earnings_timing_map():
    p = {"data": {"rows": [{"symbol": "A", "time": "time-pre-market"}, {"symbol": "B", "time": "time-after-hours"},
                           {"symbol": "C", "time": "time-not-supplied"}, {"symbol": "D"}]}}
    assert [e.timing for e in nasdaq.parse_earnings(p, date(2026, 1, 5))] == ["bmo", "amc", "unknown", "unknown"]
    assert nasdaq.parse_earnings({"data": {"rows": None}}, date(2026, 1, 3)) == []


def test_parse_dividend_calendar():
    divs = {d.symbol: d for d in nasdaq.parse_dividend_calendar(load("nasdaq_dividend_calendar.json"))}
    amgn = divs["AMGN"]
    assert (amgn.ex_date, amgn.amount, amgn.pay_date, amgn.record_date, amgn.declared_date, amgn.kind) == (
        date(2026, 11, 20), 2.52, date(2026, 12, 11), date(2026, 11, 20), date(2026, 10, 27), "cash")


def test_parse_dividend_history():
    divs = nasdaq.parse_dividend_history(load("nasdaq_dividends_AAPL.json"), "AAPL")
    assert divs[0].ex_date == date(2026, 11, 12) and divs[0].amount == pytest.approx(0.27)
    assert divs[0].declared_date == date(2026, 11, 2) and divs[0].kind == "cash"
    old = [d for d in divs if d.ex_date == date(1995, 11, 21)][0]
    assert old.declared_date is None and old.pay_date is None


def test_parse_screeners_and_universe_filter():
    from optionlens.jobs.universe import is_common_stock, select_top_stocks
    rows = nasdaq.parse_stock_screener(load("nasdaq_screener_stocks.json"))
    assert len(rows) == 300
    by = {r["symbol"]: r for r in rows}
    assert by["A"]["market_cap"] == pytest.approx(49974708660.0) and by["A"]["price"] == pytest.approx(177.26)
    assert not is_common_stock(by["ABR^D"])
    top = select_top_stocks(rows, 10)
    assert len(top) == 10 and all(is_common_stock(r) for r in top)
    assert top == sorted(top, key=lambda r: -r["market_cap"])
    etfs = nasdaq.parse_etf_screener(load("nasdaq_screener_etf.json"))
    assert etfs and {"symbol", "name", "price"} <= set(etfs[0])


@pytest.mark.parametrize("symbol, name, ok", [
    ("UNH", "UnitedHealth Group Incorporated Common Stock (DE)", True),
    ("URI", "United Rentals Inc. Common Stock", True),
    ("MPLX", "MPLX LP Common Units Representing Limited Partner Interests", True),
    ("SHEL", "Shell PLC American Depositary Shares", True),
    ("BRK.B", "Berkshire Hathaway Inc.", True),
    ("AAC.WS", "Ares Acquisition Corporation III Redeemable Warrants", False),
    ("AMPX.WS", "Amprius Technologies Inc. Warrants to purchase", False),
    ("ABCDU", "ABC Acquisition Corp Units, each consisting of one share", False),
    ("ABCDR", "ABC Acquisition Corp Rights", False),
    ("BRKRP", "Bruker Corporation 6.375% Mandatory Convertible Preferred", False),
    ("GOOGM", "Alphabet Inc. Depositary Shares representing a 1/20th interest", False),
])
def test_is_common_stock(symbol, name, ok):
    from optionlens.jobs.universe import is_common_stock
    assert is_common_stock({"symbol": symbol, "name": name, "market_cap": 1e9}) is ok


def test_parse_info_summary_profile():
    info = nasdaq.parse_info(load("nasdaq_info_AAPL.json"))
    assert info["symbol"] == "AAPL" and info["asset_class"] == "stocks" and info["price"] == pytest.approx(349.01)
    assert info["change_pct"] == pytest.approx(-0.0098)
    assert nasdaq.parse_info({"data": None}) is None
    summ = nasdaq.parse_summary(load("nasdaq_summary_AAPL.json"))
    assert summ["Sector"] == "Technology" and nasdaq.num(summ["MarketCap"]) == pytest.approx(5072167483460)
    prof = nasdaq.parse_profile(load("nasdaq_profile_AAPL.json"))
    assert prof["CompanyUrl"] == "https://www.apple.com" and prof["Address"] is None


def test_parse_financials_insiders_news_extended_surprise():
    fin = nasdaq.parse_financials(load("nasdaq_financials_AAPL.json"))
    assert set(fin) == {"income", "balance", "cashflow", "ratios"}
    inc = fin["income"]
    assert inc["periods"][0] == "9/26/2026" and inc["rows"][0]["label"] == "Total Revenue"
    assert nasdaq.num(inc["rows"][0]["values"][0]) == 478834000
    ins = nasdaq.parse_insiders(load("nasdaq_insiders_AAPL.json"))
    t = ins["trades"][0]
    assert t["insider"] == "NEWSTEAD JENNIFER" and t["shares"] == 2399 and t["date"] == date(2026, 11, 10)
    assert ins["summary"]["shares"][-1]["months3"] == -694404
    news = nasdaq.parse_news(load("nasdaq_news_AAPL.json"))
    assert news[0]["url"].startswith("https://www.nasdaq.com/articles/") and "AAPL" in news[0]["symbols"]
    ext = nasdaq.parse_extended(load("nasdaq_premarket_AAPL.json"))
    assert ext["price"] == pytest.approx(353.43) and ext["change"] == pytest.approx(0.98)
    assert ext["change_pct"] == pytest.approx(0.0028) and ext["volume"] == 2674790 and ext["high"] == 356
    sur = nasdaq.parse_earnings_surprise(load("nasdaq_earnings_surprise_AAPL.json"))
    assert sur[0] == {"fiscal_quarter": "Sep 2026", "date": date(2026, 11, 2), "eps": 2.05,
                      "consensus": 1.98, "surprise_pct": pytest.approx(0.0354)}


# ----------------------------------------------------------------------------- Treasury
def test_parse_bill_csv():
    text = ('Date,"4 WEEKS BANK DISCOUNT","4 WEEKS COUPON EQUIVALENT","13 WEEKS BANK DISCOUNT","13 WEEKS COUPON EQUIVALENT"\n'
            "11/18/2026,3.91,3.98,4.16,4.26\n11/17/2026,3.92,3.99,4.15,4.25\n11/16/2026,3.92,3.99,,\n")
    assert treasury.parse_bill_csv(text) == [(date(2026, 11, 17), pytest.approx(0.0425)),
                                             (date(2026, 11, 18), pytest.approx(0.0426))]
    with pytest.raises(ValueError):
        treasury.parse_bill_csv("Date,foo\n")


def test_parse_chain_keeps_only_standard_roots():
    payload = load("cboe_chain_AAPL.json")
    base = payload["data"]["options"][0]
    extra = [{**base, "option": o} for o in ("AAPL1261120C00100000", "B1APL261120C00100000")]
    p = {**payload, "data": {**payload["data"], "options": payload["data"]["options"] + extra}}
    assert len(cboe.parse_chain(p, "AAPL").contracts) == 3386
    spx = {**payload, "data": {**payload["data"], "options": [{**base, "option": o} for o in (
        "SPX261218C07000000", "SPXW261120C07000000", "SPXO261216C07000000")]}}
    assert [c.osi[:4] for c in cboe.parse_chain(spx, "SPX").contracts] == ["SPX2", "SPXW"]
    assert cboe.standard_roots("BRK.B") == {"BRKB"} and cboe.standard_roots("XSP") == {"XSP"}


# ----------------------------------------------------------------------------- Nasdaq calendars (splits, IPOs)
def test_parse_split_ratio_orientation():
    assert nasdaq.parse_split_ratio("8 : 1") == (8, 1, 8.0)               # forward (SNDK)
    assert nasdaq.parse_split_ratio("3:1") == (3, 1, 3.0)
    assert nasdaq.parse_split_ratio("1 : 50") == (1, 50, pytest.approx(0.02))    # reverse
    assert nasdaq.parse_split_ratio("2 : 15")[2] == pytest.approx(2 / 15)
    assert nasdaq.parse_split_ratio("5%") == (None, None, pytest.approx(1.05))   # stock dividend
    for junk in (None, "", "N/A", "0 : 3", "x:y"):
        assert nasdaq.parse_split_ratio(junk) == (None, None, None)


def test_parse_splits():
    rows = {r["symbol"]: r for r in nasdaq.parse_splits(load("nasdaq_splits.json"))}
    assert rows["SNDK"]["split_factor"] == 8 and rows["SNDK"]["execution_date"] == "2026-11-20"
    assert rows["QID"]["split_factor"] == 0.25 and rows["QID"]["ratio"] == "1 : 4"
    assert rows["YFOR"]["split_factor"] == pytest.approx(0.02) and rows["YFOR"]["new_shares"] == 1
    assert rows["LARK"]["split_factor"] == pytest.approx(1.05) and rows["LARK"]["new_shares"] is None
    assert nasdaq.parse_splits({"data": None}) == [] and nasdaq.parse_splits({"data": {"rows": None}}) == []


def test_parse_ipos():
    rows = nasdaq.parse_ipos(load("nasdaq_ipo.json"))
    by = {s: [r for r in rows if r["status"] == s] for s in ("priced", "upcoming", "filed")}
    assert [len(v) for v in by.values()] == [2, 2, 3] and len(rows) == 7          # withdrawn dropped
    mdrn = by["priced"][0]
    assert (mdrn["symbol"], mdrn["exchange"], mdrn["price_text"], mdrn["shares"], mdrn["offer_amount"],
            mdrn["date"]) == ("MDRN", "NYSE MKT", "4.25", 9411764, 39999997.0, "2026-11-13")
    assert by["upcoming"][0]["date"] == "2026-11-20" and by["upcoming"][0]["offer_amount"] == 115_000_000
    assert any(r["symbol"] is None and r["price_text"] is None for r in by["filed"])
    assert nasdaq.parse_ipos({"data": {"priced": {"rows": None}, "upcoming": {"upcomingTable": {"rows": None}}}}) == []
    rng = {"data": {"priced": {"rows": [{"proposedTickerSymbol": "x", "proposedSharePrice": "10.00-12.00",
                                         "pricedDate": "11/02/2026"}]}}}
    assert nasdaq.parse_ipos(rng)[0]["price_text"] == "10.00-12.00" and nasdaq.parse_ipos(rng)[0]["symbol"] == "X"
