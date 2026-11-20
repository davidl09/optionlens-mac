"""repo.bars close-only merge and repo.options fetch-time filter (temp DuckDB)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from optionlens import db
from optionlens.models import Bar
from optionlens.repo import bars, options
from optionlens.sources import cboe

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    yield
    db.close()


def test_upsert_closes_keeps_ohlc_and_inserts_missing(tmpdb):
    d1, d2 = date(2026, 11, 19), date(2026, 11, 20)
    bars.upsert_bars([Bar("RUT", d1, 2400.0, 2410.0, 2390.0, 2405.0, None)])
    assert bars.upsert_closes([Bar("RUT", d1, None, None, None, 2406.0, None),
                               Bar("RUT", d2, None, None, None, 2410.0, None)]) == 2
    got = bars.bars_on(d1)["RUT"]
    assert (got.open, got.high, got.low, got.close) == (2400.0, 2410.0, 2390.0, 2406.0)
    assert bars.bars_on(d2)["RUT"].close == 2410.0 and bars.bars_on(d2)["RUT"].open is None
    assert bars.upsert_closes([]) == 0


def test_fetched_since(tmpdb):
    payload = json.loads((FIX / "cboe_chain_AAPL.json").read_text())
    at = datetime(2026, 11, 20, 21, 25, tzinfo=timezone.utc)
    options.replace_option_latest(cboe.parse_chain(payload, "AAPL", fetched_at=at))
    assert options.fetched_since(at) == {"AAPL"}
    assert options.fetched_since(at, ["MSFT"]) == set()
    assert options.fetched_since(datetime(2026, 11, 20, 21, 26, tzinfo=timezone.utc)) == set()
