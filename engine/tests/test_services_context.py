"""services.context pure-ish helpers (no database)."""
from __future__ import annotations

import numpy as np

from optionlens.services.context import as_date, dedupe_roots


def _cols(osis, expiries):
    return {"osi": np.array(osis, dtype=object), "expiry": np.array(expiries, dtype="datetime64[D]"),
            "strike": np.arange(len(osis), dtype=float)}


def test_dedupe_keeps_one_root_per_expiry_preferring_pm():
    c = _cols(["SPX261218C06000000", "SPXW261218C06000000", "SPX261218P06785000", "SPXW261218P06780000",
               "SPX270115C06000000"], ["2026-12-18"] * 4 + ["2027-01-15"])
    out = dedupe_roots(c)
    assert list(out["osi"]) == ["SPXW261218C06000000", "SPXW261218P06780000", "SPX270115C06000000"]
    single = _cols(["AAPL261218C00350000", "AAPL261218P00350000"], ["2026-12-18"] * 2)
    assert dedupe_roots(single) is single


def test_dedupe_groups_by_extra_key():
    c = _cols(["SPX261218C06000000", "SPXW261218C06000000", "SPX261218C06000000"], ["2026-12-18"] * 3)
    c["date"] = np.array(["2026-11-17", "2026-11-17", "2026-11-18"], dtype="datetime64[D]")
    assert list(dedupe_roots(c, "date")["date"].astype(str)) == ["2026-11-17", "2026-11-18"]


def test_as_date():
    assert str(as_date(np.datetime64("2026-11-19"))) == "2026-11-19" and as_date(np.datetime64("NaT")) is None
    assert str(as_date("2026-11-19T10:00:00")) == "2026-11-19"
