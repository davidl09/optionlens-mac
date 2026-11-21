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


def _leveraged_etf(sym: str, ex: date, factor: float, adjusted: bool) -> list[Bar]:
    """A SOXS-like decaying ETF: 60 sessions, -1.5%/day, a 1-for-(1/factor) reverse split at `ex`; `adjusted`
    = history before ex already divided by the factor (as Nasdaq serves it after the split)."""
    from datetime import timedelta
    out, d, px = [], ex - timedelta(days=60), 100.0
    while d < ex + timedelta(days=20):
        if d.weekday() < 5:
            p = px if d >= ex or adjusted else px * factor          # pre-split shares traded at a fraction
            out.append(Bar(sym, d, p * 1.01, p * 1.03, p * 0.98, p, int(1e6 if d >= ex or adjusted else 1e6 / factor)))
            px *= 0.985
        d += timedelta(days=1)
    return out


def test_split_adjustment_applies_once_and_never_to_adjusted_history(tmpdb):
    from optionlens.jobs.prices import apply_splits
    from optionlens.analytics import stats
    ex = date(2026, 10, 20)
    bars.upsert_bars(_leveraged_etf("SOXS", ex, 1 / 15, adjusted=False))
    bars.upsert_bars(_leveraged_etf("SOXL", ex, 1 / 15, adjusted=True))
    split = [{"symbol": s, "execution_date": ex.isoformat(), "split_factor": 1 / 15, "ratio": "1 : 15"} for s in ("SOXS", "SOXL")]
    split.append({"symbol": "SOXS", "execution_date": "2027-01-04", "split_factor": 2.0, "ratio": "2 : 1"})   # not yet
    assert apply_splits(split, date(2026, 11, 21)) == ["SOXS 1 : 15 2026-10-20"]
    assert apply_splits(split, date(2026, 11, 21)) == []                     # idempotent
    a, b = bars.get_bars("SOXS"), bars.get_bars("SOXL")
    assert a["close"] == pytest.approx(b["close"]) and a["high"] == pytest.approx(b["high"])
    assert a["volume"].tolist() == pytest.approx(b["volume"].tolist(), rel=1e-6)
    r = stats.returns_row(a["date"].astype("datetime64[D]").astype(object).tolist(), a["close"])
    assert r["m1"] == pytest.approx(0.985 ** 21 - 1, rel=0.05)               # a decay, not a 15x "gain"
    hi, lo = stats.high_low(a["high"], a["low"], a["close"], 252)
    assert hi / lo < 3


def test_split_check_ignores_ordinary_moves_and_tiny_factors():
    from optionlens.analytics.splits import shows_unadjusted_split
    assert shows_unadjusted_split(10.0, 100.5, 0.1)          # 1:10 reverse split, unadjusted
    assert not shows_unadjusted_split(100.0, 100.5, 0.1)     # already adjusted
    assert shows_unadjusted_split(300.0, 151.0, 2.0)         # 2:1 forward split, unadjusted
    assert not shows_unadjusted_split(104.9, 49.2, None)      # no calendar factor: a -53% day is a move
    assert not shows_unadjusted_split(100.0, 95.0, 1.05)     # 5% stock dividend: indistinguishable
    assert not shows_unadjusted_split(100.0, 20.0, 2.0)      # a crash far beyond the split ratio
