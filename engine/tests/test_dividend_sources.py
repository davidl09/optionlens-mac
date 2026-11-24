"""Dividends of every US listing (docs/INTEGRATION.md §6 "Dividends"): the dividends behind IBKR's adjusted closes
(analytics.dividends.from_adjusted_closes) on synthetic series built with IBKR's documented method, the merge of the
sources per (symbol, ex-date) with the discrepancy flag (repo.dividends), and the ingest + wants round trip."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from optionlens import db
from optionlens.analytics import calendar as cal
from optionlens.analytics import dividends as dv
from optionlens.models import Dividend
from optionlens.repo import dividends as repo

NOW = datetime(2026, 11, 24, 21, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------- IBKR's adjustment, synthetically
def ibkr_adjusted(raw: list[tuple[date, float]], divs: dict[date, float]) -> list[tuple[date, float]]:
    """ADJUSTED_LAST as IBKR documents it (CRSP): on each ex-date t every earlier close is multiplied by
    (1 − D_t / C_{t−1}), C the (split-adjusted) TRADES close."""
    closes = dict(raw)
    days = [d for d, _ in raw]
    factor = {d: 1.0 for d in days}
    for t, amount in divs.items():
        i = days.index(t)
        f = 1.0 - amount / closes[days[i - 1]]
        for d in days[:i]:
            factor[d] *= f
    return [(d, closes[d] * factor[d]) for d in days]


def series(start: date, end: date, first: float = 60.0, seed: int = 7) -> list[tuple[date, float]]:
    days = cal.trading_days(start, end)
    rng = np.random.default_rng(seed)
    px = first * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
    return [(d, round(float(p), 2)) for d, p in zip(days, px)]


def quarterly_ex_dates(start: date, end: date) -> list[date]:
    """KO-like ex-dates: the first trading day on/after the 14th of Mar, Jun, Sep, Nov."""
    out = []
    for y in range(start.year, end.year + 1):
        for m in (3, 6, 9, 11):
            d = date(y, m, 14)
            while not cal.is_trading_day(d):
                d += timedelta(days=1)
            if start < d <= end:
                out.append(d)
    return out


KO_START, KO_END = date(2023, 1, 3), date(2026, 11, 20)


def test_ko_quarterly_dividends_are_recovered_exactly():
    raw = series(KO_START, KO_END)
    paid = {d: 0.51 for d in quarterly_ex_dates(KO_START, KO_END)}
    got = dv.from_adjusted_closes(raw, ibkr_adjusted(raw, paid))
    assert [d for d, _ in got] == sorted(paid)
    assert all(a == pytest.approx(0.51, abs=1e-4) for _, a in got)


def test_the_formula_is_close_before_times_one_minus_the_ratio_step():
    """D_t = C_{t−1} · (1 − r_{t−1} / r_t), r = adjusted / raw (the ratio steps UP at the ex-date)."""
    raw = [(date(2026, 3, 12), 60.0), (date(2026, 3, 13), 59.8), (date(2026, 3, 16), 59.5)]
    adj = ibkr_adjusted(raw, {date(2026, 3, 16): 0.51})
    r = [a / c for (_, c), (_, a) in zip(raw, adj)]
    assert r[2] > r[1] and r[0] == pytest.approx(r[1])
    assert 59.8 * (1 - r[1] / r[2]) == pytest.approx(0.51)
    assert dv.from_adjusted_closes(raw, adj) == [(date(2026, 3, 16), 0.51)]


def test_a_split_is_ignored_and_older_amounts_are_in_todays_shares():
    """A 2:1 split on 2025-06-02: IBKR split-adjusts both series, so the ratio does not step there; a dividend paid
    before the split comes out in today's (post-split) shares: half the amount paid then."""
    split = date(2025, 6, 2)
    actual = series(KO_START, KO_END, first=120.0)
    actual = [(d, p if d < split else round(p / 2, 2)) for d, p in actual]          # the traded prices
    raw = [(d, p / 2 if d < split else p) for d, p in actual]                       # TRADES: split-adjusted
    ex = quarterly_ex_dates(KO_START, KO_END)
    paid = {d: (1.02 if d < split else 0.51) for d in ex}                           # as paid per share then
    adjusted_amounts = {d: (a / 2 if d < split else a) for d, a in paid.items()}
    got = dict(dv.from_adjusted_closes(raw, ibkr_adjusted(raw, adjusted_amounts)))
    assert split not in got and sorted(got) == ex
    assert all(a == pytest.approx(0.51, abs=1e-4) for a in got.values())
    # a series that lacks the split adjustment shows a 50 % step there: never a dividend
    unadjusted = dv.from_adjusted_closes(actual, ibkr_adjusted(raw, adjusted_amounts))
    assert split not in dict(unadjusted)


def test_a_special_dividend_is_derived_and_flagged_as_before():
    raw = series(KO_START, KO_END)
    ex = quarterly_ex_dates(KO_START, KO_END)
    special = date(2025, 12, 1)
    paid = {**{d: 0.51 for d in ex}, special: 2.0}                                   # ≥ 2.5 × the usual 0.51
    got = dv.from_adjusted_closes(raw, ibkr_adjusted(raw, paid))
    assert dict(got)[special] == pytest.approx(2.0, abs=1e-4)
    divs = [Dividend("KO", d, a, None, None, None, "cash", source="ibkr-adjusted") for d, a in got]
    assert dv.one_offs(divs) == {(special, dict(got)[special])}
    assert dv.annual_rate(divs, KO_END) == pytest.approx(4 * 0.51, abs=1e-3)      # the special is left out


def test_noise_and_cent_rounding_make_no_dividends():
    raw = series(KO_START, KO_END)
    ex = quarterly_ex_dates(KO_START, KO_END)
    adj = ibkr_adjusted(raw, {d: 0.51 for d in ex})
    tiny = [(d, a * (1 + 1e-6 * ((i % 3) - 1))) for i, (d, a) in enumerate(adj)]     # float noise < $0.001
    assert [d for d, _ in dv.from_adjusted_closes(raw, tiny)] == ex
    rounded = [(d, round(a, 2)) for d, a in adj]                                    # cent-rounded adjusted closes
    got = dv.from_adjusted_closes(raw, rounded)
    assert [d for d, _ in got] == ex and all(abs(a - 0.51) < 0.015 for _, a in got)
    assert dv.from_adjusted_closes(raw, raw) == []                                 # no adjustment: nothing paid
    assert dv.from_adjusted_closes([], []) == []


# ----------------------------------------------------------------------------- the merge
@pytest.fixture
def tmpdb(tmp_path):
    db.close()
    db.connect(tmp_path / "t.duckdb")
    yield
    db.close()


def _d(sym, ex, amt, source=None, pay=None, declared=None):
    return Dividend(sym, ex, amt, pay, None, declared, "cash", source=source)


def test_merge_prefers_nasdaq_then_ibkr_then_questrade_and_flags_discrepancies(tmpdb):
    a, b, c, e = date(2026, 3, 13), date(2026, 6, 12), date(2026, 9, 11), date(2026, 12, 1)
    repo.upsert_dividends([_d("KO", a, 0.51, pay=date(2026, 4, 1), declared=date(2026, 2, 20)),
                           _d("KO", b, 0.51)])
    repo.upsert_derived([_d("KO", a, 0.53, "ibkr-adjusted"),                       # |Δ| 0.02 > 0.01: flagged
                         _d("KO", b, 0.515, "ibkr-adjusted"),                      # within the tolerance
                         _d("KO", c, 0.51, "ibkr-adjusted"),
                         _d("KO", c, 0.51, "questrade", pay=date(2026, 10, 1)),
                         _d("KO", e, 0.53, "questrade", pay=date(2026, 12, 15))], NOW)
    got = {d.ex_date: d for d in repo.for_symbol("KO")}
    assert [(got[x].source, got[x].amount, got[x].discrepancy) for x in (a, b, c, e)] == [
        ("nasdaq", 0.51, True), ("nasdaq", 0.51, False), ("ibkr-adjusted", 0.51, False), ("questrade", 0.53, False)]
    assert got[a].pay_date == date(2026, 4, 1) and got[a].declared_date == date(2026, 2, 20)
    assert got[c].pay_date == date(2026, 10, 1)                     # IBKR's row, its pay date from Questrade's
    assert [d.ex_date for d in repo.between(date(2026, 9, 20), date(2026, 10, 5), by="pay_date")] == [c]
    assert repo.next_ex(date(2026, 11, 1))["KO"].source == "questrade"
    assert repo.trailing_sum(date(2026, 11, 1))["KO"] == pytest.approx(0.51 * 3)
    assert repo.symbols_with_history(covering=date(2026, 6, 30)) == {"KO"}
    assert repo.by_symbol(["KO"], date(2026, 6, 1))["KO"][0].ex_date == b


def test_a_refetch_replaces_what_it_derived_in_its_span(tmpdb):
    repo.upsert_derived([_d("X", date(2026, 3, 2), 0.2, "ibkr-adjusted"), _d("X", date(2026, 6, 1), 0.2, "ibkr-adjusted"),
                         _d("X", date(2026, 6, 1), 0.2, "questrade")], NOW)
    repo.replace_derived("X", "ibkr-adjusted", date(2026, 5, 1), date(2026, 11, 1),
                         [_d("X", date(2026, 9, 1), 0.21, "ibkr-adjusted")], NOW)
    assert [(d.ex_date, d.source) for d in repo.for_symbol("X")] == [
        (date(2026, 3, 2), "ibkr-adjusted"), (date(2026, 6, 1), "questrade"), (date(2026, 9, 1), "ibkr-adjusted")]
    with pytest.raises(ValueError):
        repo.upsert_derived([_d("X", date(2026, 1, 2), 0.2)], NOW)                  # Nasdaq rows go to upsert_dividends
