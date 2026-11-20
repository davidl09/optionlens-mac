"""Average / relative option volume with short collected history (fresh install) and the interim
unusual-option-volume criterion."""
from __future__ import annotations

import time
from datetime import date

import numpy as np
import pytest

from optionlens.analytics import stats
from optionlens.services.reports import options as opt_reports
from optionlens.services.reports import universe
from optionlens.services.reports.base import RunParams

NAN = np.nan


def test_prior_option_volume_avg_uses_whatever_prior_days_exist():
    assert stats.prior_option_volume_avg(np.array([]), np.array([])) == (None, 0)          # never fabricated
    assert stats.prior_option_volume_avg(np.array([100.0]), np.array([50.0])) == (150.0, 1)
    avg, days = stats.prior_option_volume_avg(np.array([100.0, NAN, NAN]), np.array([NAN, NAN, 20.0]))
    assert (avg, days) == (60.0, 2)                                     # all-unknown day skipped
    cv = np.arange(30, dtype=float)
    assert stats.prior_option_volume_avg(cv, np.zeros(30)) == (pytest.approx(np.mean(cv[-20:])), 20)


def _frame(days, rel, vol, oi):
    n = len(days)
    keys = {c.key for c in opt_reports.UOV_COLS} | {"rel_option_volume", "opt_history_days", "option_volume", "total_oi",
                                                    "kind", "in_universe", "price", "market_cap"}
    cols = {k: np.full(n, NAN) for k in keys}
    cols.update(symbol=np.array([f"S{i}" for i in range(n)], dtype=object), name=np.full(n, "x", dtype=object),
                kind=np.full(n, "stock", dtype=object), in_universe=np.ones(n, bool),
                opt_history_days=np.array(days, float), rel_option_volume=np.array(rel, float),
                option_volume=np.array(vol, float), total_oi=np.array(oi, float))
    for k in ("sector", "industry", "sentiment", "earnings_timing"):
        cols[k] = np.full(n, None, dtype=object)
    return universe.Frame(date(2026, 11, 19), date(2026, 11, 19), None, cols, 1, int(max(days)), time.monotonic())


def _run(monkeypatch, f, **p):
    monkeypatch.setattr(opt_reports, "frame", lambda: f)
    monkeypatch.setattr(opt_reports, "frame_output", _no_watchlist(opt_reports.frame_output))
    params = RunParams({"min_rel_volume": 2.0, "min_volume": 1000, "min_vol_oi": 0.5, "lookback_days": 0,
                        "kind": "all", **p})
    return opt_reports.run_uov(params)


def _no_watchlist(fn):
    def wrapped(f, m, cols, **kw):
        kw.setdefault("extra", {})["in_watchlist"] = np.zeros(len(f), bool)
        return fn(f, m, cols, **kw)
    return wrapped


def test_uov_interim_criterion_on_fresh_install(monkeypatch):
    # no prior day anywhere: qualify on volume / OI ≥ 0.5 (S0: 0.8, S1: 0.1, S2: too little volume)
    out = _run(monkeypatch, _frame([0, 0, 0], [NAN, NAN, NAN], [8000, 2000, 500], [10000, 20000, 100]))
    assert [r["symbol"] for r in out.rows] == ["S0"]
    r = out.rows[0]
    assert r["criterion"] == opt_reports.CRIT_INTERIM and r["vol_oi"] == pytest.approx(0.8)
    assert r["avg_days"] == 0 and r["rel_volume"] is None and r["avg_option_volume_20d"] is None
    assert any("Interim criterion for 1 symbol" in n for n in out.notes)


def test_uov_partial_average_and_mixed_rows(monkeypatch):
    # S0 has 3 prior days (rel 2.5 qualifies), S1 rel 1.2 does not, S2 is new (interim, vol/OI 0.9)
    out = _run(monkeypatch, _frame([3, 3, 0], [2.5, 1.2, NAN], [5000, 5000, 9000], [50000, 50000, 10000]))
    assert [(r["symbol"], r["criterion"], r["avg_days"]) for r in out.rows] == [
        ("S0", opt_reports.CRIT_REL, 3), ("S2", opt_reports.CRIT_INTERIM, 0)]
    assert any("uses the 3 prior collected day(s)" in n for n in out.notes)
