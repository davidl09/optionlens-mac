"""Canadian listings on Questrade: symbol resolution without US collisions and Montréal Exchange chains, on the
documentation's samples (tests/fixtures/questrade/doc_*.json; the symbols/:id/options sample is Bank of Montreal's
MX chain: listingExchange "MX", root "BMO", multiplier 100, American exercise)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from optionlens.sources.osi import format_osi, parse_osi, standard_roots
from optionlens.sources.questrade import mapping

FIX = Path(__file__).parent / "fixtures" / "questrade"


def doc(name: str) -> dict:
    return json.loads((FIX / f"doc_{name}.json").read_text())


def test_search_keeps_the_us_and_the_toronto_listing_apart():
    s = doc("symbols_search")       # BMO (NYSE, USD, 9292) and BMO.PRJ.TO (TSX, CAD, 9300)
    assert mapping.pick_symbol_id(s, "BMO") == 9292
    assert mapping.pick_symbol_id(s, "BMO.PRJ.TO") == 9300
    assert mapping.pick_symbol_id(s, "BMO.TO") is None          # never the NYSE listing, never the preferred share
    usd_on_tsx = {"symbols": [{"symbol": "DLR.U.TO", "symbolId": 5, "listingExchange": "TSX", "currency": "USD"}]}
    assert mapping.pick_symbol_id(usd_on_tsx, "DLR.U.TO") is None   # USD units on the TSX are not in the CAD market
    venture = {"symbols": [{"symbol": "ABC.VN", "symbolId": 6, "listingExchange": "TSXV", "currency": "CAD"}]}
    assert mapping.pick_symbol_id(venture, "ABC.VN") == 6


def test_mx_chain_structure_maps_to_exchange_qualified_codes():
    expiries, listed = mapping.listed_contracts(doc("symbols_options"), "BMO.TO")
    assert expiries == {date(2015, 1, 17): "2015-01-17T00:00:00.000000-05:00"}
    assert len(listed) == 6
    c = listed[6101993]
    assert (c.expiry, c.strike, c.right) == (date(2015, 1, 17), 60.0, "C")
    osi = format_osi("BMO.TO", c.expiry, c.right, c.strike)
    assert osi == "BMO.TO150117C00060000"
    assert osi != format_osi("BMO", c.expiry, c.right, c.strike) == "BMO150117C00060000"   # OPRA's code
    assert parse_osi(osi) == ("BMO.TO", date(2015, 1, 17), "C", 60.0)


def test_mx_roots_are_the_ticker_before_class_or_unit_suffix():
    assert standard_roots("TECK.B.TO") == {"TECK"}
    assert standard_roots("AP.UN.TO") == {"AP"}
    assert standard_roots("BRK.B") == {"BRKB"}                  # US unchanged
    payload = {"optionChain": [{"expiryDate": "2026-12-18T00:00:00.000000-05:00", "listingExchange": "MX",
                                "chainPerRoot": [
                                    {"optionRoot": "TECK", "multiplier": 100,
                                     "chainPerStrikePrice": [{"strikePrice": 60.5, "callSymbolId": 1, "putSymbolId": 2}]},
                                    {"optionRoot": "TECK1", "multiplier": 100,      # adjusted contract: dropped
                                     "chainPerStrikePrice": [{"strikePrice": 60.5, "callSymbolId": 3, "putSymbolId": 4}]}]}]}
    _, listed = mapping.listed_contracts(payload, "TECK.B.TO")
    assert sorted(listed) == [1, 2]
    assert format_osi("TECK.B.TO", date(2026, 12, 18), "P", 60.5) == "TECK.B.TO261218P00060500"


def test_mx_option_quote_maps_like_opra():
    _, listed = mapping.listed_contracts(doc("symbols_options"), "BMO.TO")
    q = {"symbolId": 6101993, "bidPrice": 1.2, "askPrice": 1.3, "lastTradePrice": 1.25, "volume": 10,
         "openInterest": 120, "volatility": 21.5, "delta": 0.55, "gamma": 0.08, "theta": -0.02, "vega": 0.07,
         "rho": 0.01, "delay": 0}
    row = mapping.contract("BMO.TO", listed[6101993], q)
    assert row.symbol == "BMO.TO" and row.osi == "BMO.TO150117C00060000"
    assert (row.bid, row.ask, row.iv, row.oi) == (1.2, 1.3, 0.215, 120)


def test_symbol_info_reads_the_universe_fields():
    i = mapping.symbol_info(doc("symbols_id"))
    assert (i.has_options, i.prev_close, i.market_cap, i.listing_exchange) == (True, 102.5, 613756367500, "NASDAQ")
    assert abs(i.dividend_yield - 0.0184) < 1e-12 and i.security_type == "Stock" and i.name == "APPLE INC"
