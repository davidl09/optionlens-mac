"""Repo query functions added for the reports layer: month-end closes and latest-vs-EOD OI changes."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from optionlens import db
from optionlens.models import Bar
from optionlens.repo import bars, options, quotes
from optionlens.sources import cboe

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmpdb(tmp_path):
    prev = db.path()
    db.close()
    db.connect(tmp_path / "t.duckdb")
    yield
    db.close()
    if prev is not None:
        db.connect(prev)


def test_month_end_closes(tmpdb):
    bs = [Bar("A", date(2026, 1, 1) + timedelta(days=i), None, None, None, 100.0 + i, 1) for i in range(70)]
    bars.upsert_bars(bs + [Bar("B", date(2026, 2, 27), None, None, None, 5.0, 1)])
    c = bars.month_end_closes(date(2026, 1, 15))
    got = list(zip(c["symbol"].tolist(), c["year"].tolist(), c["month"].tolist(), c["close"].tolist()))
    assert got == [("A", 2026, 1, 130.0), ("A", 2026, 2, 158.0), ("A", 2026, 3, 169.0), ("B", 2026, 2, 5.0)]
    assert len(bars.month_end_closes(date(2026, 1, 1), ["B"])["close"]) == 1


def test_latest_vs_eod_oi(tmpdb):
    t = datetime(2026, 11, 19, 21, 30, tzinfo=timezone.utc)
    snap = cboe.parse_chain(json.loads((FIX / "cboe_chain_AAPL.json").read_text()), "AAPL", fetched_at=t)
    options.replace_option_latest(snap)
    quotes.upsert_quotes([snap.quote])
    prior = date(2026, 11, 18)
    options.write_option_eod(prior, t - timedelta(minutes=1))
    with db.write() as cur:
        cur.execute("UPDATE option_eod SET oi = oi - 1000 WHERE osi IN (SELECT osi FROM option_eod WHERE oi > 5000 "
                    "ORDER BY osi LIMIT 3)")
    c = options.latest_vs_eod_oi(prior, min_abs_change=500)
    assert len(c["osi"]) == 3 and (c["oi"] - c["oi_prev"] == 1000).all()
    assert c["underlying"][0] == snap.quote.price
    assert len(options.latest_vs_eod_oi(prior, min_abs_change=0, limit=10)["osi"]) == 10
    assert len(options.latest_vs_eod_oi(prior, ["MSFT"], 0)["osi"]) == 0
    assert len(options.latest_vs_eod_oi(date(2026, 1, 1))["osi"]) == 0
