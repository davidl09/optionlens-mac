"""The reports' frame takes each market's own latest session: a TSX session on a US holiday (2026-11-26,
Thanksgiving) does not hide the US symbols' last session (2026-11-25), and the other way round."""
from __future__ import annotations

from datetime import date

from optionlens.analytics import markets
from optionlens.repo import metrics as metrics_repo
from optionlens.services.reports import universe

from .test_chain_sources import tmpdb  # noqa: F401  (fixture)


def test_each_market_keeps_its_latest_session(tmpdb):
    metrics_repo.upsert_metrics(date(2026, 11, 24), "AAPL", {"iv30": 0.2})
    metrics_repo.upsert_metrics(date(2026, 11, 25), "AAPL", {"iv30": 0.3})
    metrics_repo.upsert_metrics(date(2026, 11, 26), "RY.TO", {"iv30": 0.15})
    assert universe.market_dates() == {markets.US: date(2026, 11, 25), markets.CA: date(2026, 11, 26)}
