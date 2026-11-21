"""Ingested IV history (vol_history) and the merged IV30 rule (collected wins, else ingested) used by
IV rank, vol history and the IV proxy; bridge status rows."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from optionlens import db
from optionlens.repo import bridge, metrics, vol_history
from optionlens.services import context

D0 = date(2026, 1, 1)


@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    context.invalidate()
    yield
    context.invalidate()
    db.close()


def _days(n: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def test_upsert_is_idempotent_and_validates_kind(tmpdb):
    pts = [(d, 0.2 + i / 1000) for i, d in enumerate(_days(5))]
    assert vol_history.upsert("AAPL", "iv30", "ibkr", pts) == 5
    assert vol_history.upsert("AAPL", "iv30", "ibkr", pts[:2] + [(D0, 0.5)]) == 2   # (date) deduped, last wins
    assert vol_history.last_dates("iv30") == {"AAPL": D0 + timedelta(days=4)}
    assert vol_history.last_dates("hv30") == {}
    h = vol_history.history_merged("AAPL", ["iv30"])
    assert len(h["date"]) == 5 and h["iv30"][0] == 0.5
    with pytest.raises(ValueError):
        vol_history.upsert("AAPL", "iv90", "ibkr", pts)


def test_collected_iv30_wins_ingested_fills_gaps(tmpdb):
    metrics.upsert_metrics(D0 + timedelta(days=1), "AAPL", {"iv30": 0.30, "hv30": 0.25})
    metrics.upsert_metrics(D0 + timedelta(days=2), "AAPL", {"hv30": 0.26})          # chain had no iv30
    vol_history.upsert("AAPL", "iv30", "ibkr", [(d, 0.20) for d in _days(4)])
    h = vol_history.history_merged("AAPL", ["iv30", "hv30"])
    assert [str(d) for d in h["date"]] == [str(d) for d in _days(4)]
    assert h["iv30"].tolist() == [0.20, 0.30, 0.20, 0.20]                           # collected 0.30 kept
    assert np.isnan(h["hv30"][0]) and h["hv30"][1] == 0.25
    bulk = vol_history.iv30_bulk(2)
    assert list(bulk) == ["AAPL"] and bulk["AAPL"]["iv30"].tolist() == [0.20, 0.20]
    assert vol_history.latest_iv30_date() == D0 + timedelta(days=3)
    assert context.metrics_series("AAPL", ["iv30"])["iv30"][D0 + timedelta(days=1)] == 0.30


def test_iv_rank_uses_ingested_history(tmpdb):
    days = _days(60)
    vol_history.upsert("MSFT", "iv30", "ibkr", [(d, 0.10 + i / 590) for i, d in enumerate(days)])
    r = context.iv_rank_info("MSFT")
    assert r["history_days"] == 60 and r["iv_rank"] == pytest.approx(1.0) and r["iv_percentile"] > 0.98
    assert context.iv_rank_all()["MSFT"]["iv_rank"] == pytest.approx(1.0)
    vol_history.upsert("AAPL", "iv30", "ibkr", [(days[-1] + timedelta(days=1), 0.3)])
    context.invalidate()
    assert context.iv_rank_info("MSFT")["iv_rank"] is None                          # stale: not on the latest session


def test_iv_proxy_reads_ingested_iv30(tmpdb):
    vol_history.upsert("MSFT", "iv30", "ibkr", [(D0, 0.33)])
    assert context.DbPriceProvider().iv_proxy(D0, "MSFT", 45) == pytest.approx(0.33)


def test_bridge_status_round_trip(tmpdb):
    assert bridge.get_status() is None
    t = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    bridge.record_push(t)
    bridge.record_heartbeat("optionlens-mac 0.1.0", True, "paper", t + timedelta(seconds=5))
    s = bridge.get_status()
    assert s == {"client": "optionlens-mac 0.1.0", "ibkr_connected": True, "account_type": "paper",
                 "heartbeat_at": t + timedelta(seconds=5), "last_push_at": t}


def test_current_iv30_falls_back_to_the_merged_history_with_its_date(tmpdb):
    days = _days(30)
    vol_history.upsert("DELL", "iv30", "ibkr", [(d, 0.20 + i / 100) for i, d in enumerate(days)])   # max on the last day
    r = context.iv_rank_info("DELL")
    assert r["iv30"] == pytest.approx(0.49) and r["iv30_asof"] == days[-1]       # no collected IV30: the ingested one
    assert r["iv_rank"] == pytest.approx(1.0) and r["iv_percentile"] == pytest.approx(29 / 30)   # rank of that value
    metrics.upsert_metrics(days[-1] + timedelta(days=1), "DELL", {"hv30": 0.3})   # newest row (stats only): no IV30
    r = context.iv_rank_info("DELL")
    assert r["iv30"] == pytest.approx(0.49) and r["iv30_asof"] == days[-1]       # still the latest stored IV30 ...
    assert r["iv_rank"] is None and r["iv_percentile"] is None                   # ... not the last session's: no rank
    metrics.upsert_metrics(days[-1] + timedelta(days=1), "DELL", {"iv30": 0.25})  # collected IV30 of that session wins
    r = context.iv_rank_info("DELL")
    assert r["iv30"] == 0.25 and r["iv30_asof"] == days[-1] + timedelta(days=1) and r["iv_rank"] == pytest.approx(0.05 / 0.29)
    none = context.iv_rank_info("NONE")                                          # no history: unknown, never 0
    assert none["iv30"] is None and none["iv30_asof"] is None and none["iv_rank"] is None
