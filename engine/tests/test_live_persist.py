"""Live values -> stored latest data (live/persist.py): quotes_latest field-wise, the served option_latest rows' market
fields (fetch time, source and delayed of the chain kept), never the end-of-day archive."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from optionlens import db
from optionlens.live import persist
from optionlens.live.resolve import resolve
from optionlens.models import Quote
from optionlens.repo import options as options_repo
from optionlens.repo import quotes as quotes_repo
from optionlens.repo._sql import scalar
from optionlens.sources import cboe

FIX = Path(__file__).parent / "fixtures"
FETCHED = datetime(2026, 11, 19, 19, 0, tzinfo=timezone.utc)


@pytest.fixture
def chain(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    snap = cboe.parse_chain(json.loads((FIX / "cboe_chain_AAPL.json").read_text()), "AAPL", fetched_at=FETCHED)
    options_repo.replace_option_latest(snap, "ibkr")
    quotes_repo.upsert_quotes([Quote("AAPL", FETCHED, 349.0, 1.0, 0.003, 345.0, 350.0, 344.0, 348.0, 1000, 348.9,
                                     349.1, 0.25, False)], "ibkr")
    yield snap
    db.close()


def test_store_merges_quotes_and_patches_served_rows(chain):
    osi = chain.contracts[0].osi
    at = FETCHED.timestamp() + 60
    persist.store([{"symbol": "AAPL", "price": 350.5, "bid": 350.4, "source": "questrade", "delayed": False, "_at": at}],
                  [{"osi": osi, "bid": 9.9, "ask": 10.1, "iv": 0.3, "source": "questrade", "_at": at},
                   {"osi": "NOPE261218C00100000", "bid": 1.0, "source": "questrade", "_at": at}])
    q = quotes_repo.get_quote("AAPL")
    assert (q.price, q.bid, q.ask, q.prev_close, q.iv30) == (350.5, 350.4, 349.1, 348.0, 0.25)   # field-wise
    assert quotes_repo.source_of("AAPL") == "questrade" and q.ts.timestamp() == at
    row = next(c for c in options_repo.get_chain("AAPL").contracts if c.osi == osi)
    assert (row.bid, row.ask, row.iv, row.iv_source) == (9.9, 10.1, 0.3, "questrade")
    assert options_repo.latest_fetched_at("AAPL") == FETCHED                     # the chain's fetch time is kept
    assert options_repo.latest_source("AAPL") == "ibkr"
    assert scalar("SELECT count(*) FROM option_eod") == 0                       # never archived
    # an older live value does not replace a newer stored quote
    persist.store([{"symbol": "AAPL", "price": 1.0, "source": "questrade", "delayed": False, "_at": at - 3600}], [])
    assert quotes_repo.get_quote("AAPL").price == 350.5


def test_resolve_maps_osis_to_underlyings(chain):
    osi = chain.contracts[0].osi
    expiry = chain.contracts[0].expiry
    w = resolve([" aapl "], [osi.lower(), "SPXW261218C06000000", "garbage"], [("AAPL", expiry, None, None)])
    assert w.symbols == {"AAPL"} and w.contracts[osi] == "AAPL" and w.contracts["SPXW261218C06000000"] == "SPX"
    assert "GARBAGE" not in w.contracts
    assert {o for o, u in w.contracts.items() if u == "AAPL"} == {c.osi for c in chain.contracts if c.expiry == expiry}
