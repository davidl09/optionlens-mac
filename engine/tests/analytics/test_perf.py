"""Performance guard: a full-universe vertical-spread screen (1000 symbols × 1000 contracts)."""
import time
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from optionlens.analytics import bs, strategies as st
from optionlens.analytics.calendar import NY
from optionlens.analytics.chain import ChainArrays

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=NY)
EXPS = [date(2026, 1, 9) + timedelta(weeks=i) for i in range(10)]


def _chain(seed: int) -> ChainArrays:
    rng = np.random.default_rng(seed)
    spot = float(rng.uniform(20, 500))
    ks = np.round(spot * np.linspace(0.75, 1.25, 50), 2)
    E, K, Rt = (a.ravel() for a in np.meshgrid(np.arange(10), ks, [0, 1], indexing="ij"))
    T = np.array([(EXPS[e] - NOW.date()).days / 365 for e in E])
    iv = 0.3 + 0.2 * (1 - K / spot)
    right = np.where(Rt == 0, "C", "P")
    p = bs.price(spot, K, T, 0.04, 0.0, iv, right)
    g = bs.greeks(spot, K, T, 0.04, 0.0, iv, right)
    return ChainArrays.from_columns(
        expiry=[EXPS[e] for e in E], strike=K, right=right, bid=np.maximum(p - 0.05, 0.0), ask=p + 0.05,
        last=p, volume=rng.integers(0, 500, len(K)), oi=rng.integers(0, 5000, len(K)), iv=iv,
        delta=g["delta"], gamma=g["gamma"], theta=g["theta"], vega=g["vega"], spot=spot, asof=NOW,
        symbol=f"S{seed}", r=0.04, hv30=0.25)


@pytest.mark.perf
def test_vertical_screen_full_universe_under_5s():
    chains = [_chain(i) for i in range(1000)]
    assert sum(len(c) for c in chains) == 1_000_000
    params = st.ScreenParams(dte_min=7, dte_max=60, min_oi=10, max_spread_pct=0.5, delta_min=0.15, delta_max=0.45)
    t0 = time.perf_counter()
    rows, truncated = st.screen_universe("bull-put-spreads", chains, params, sort_key="ev_hv", limit=5000)
    elapsed = time.perf_counter() - t0
    assert truncated and len(rows) == 5000
    assert elapsed < 5.0, f"vertical screen took {elapsed:.2f}s"
