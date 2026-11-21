"""Report registry: every SPEC §7 id is registered, every ReportDef is well-formed, and every report
runs on the seeded DB returning rows that carry all its column keys (JSON-safe)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from optionlens.services.reports import registry
from optionlens.services.reports.base import CATEGORIES, FMTS, PARAM_TYPES, ParamError

from .seed_reports import api, seeded  # noqa: F401 - fixtures

SPEC = Path(__file__).resolve().parents[3] / "docs" / "SPEC.md"


def spec_report_ids() -> set[str]:
    text = SPEC.read_text()
    sec = text[text.index("## 7. Reports"):text.index("## 8. Per-symbol")]
    return set(re.findall(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`", sec))


def test_every_spec_report_is_registered():
    ids = spec_report_ids()
    assert len(ids) >= 60
    assert ids - set(registry.REPORTS) == set()


@pytest.mark.parametrize("rid", sorted(registry.REPORTS))
def test_report_def_is_well_formed(rid):
    d = registry.REPORTS[rid]
    js = d.as_dict()
    assert set(js) <= {"id", "title", "category", "description", "params", "columns", "default_sort"}
    assert js["category"] in CATEGORIES and js["title"] and js["description"]
    keys = [c["key"] for c in js["columns"]]
    assert len(keys) == len(set(keys)), "duplicate column keys"
    assert all(c["fmt"] in FMTS for c in js["columns"])
    if d.default_sort:
        assert d.default_sort[0] in keys and d.default_sort[1] in ("asc", "desc")
    pkeys = [p["key"] for p in js["params"]]
    assert len(pkeys) == len(set(pkeys))
    for prm, pj in zip(d.params, js["params"]):
        assert pj["type"] in PARAM_TYPES and "default" in pj
        assert prm.parse(None) == prm.default_value()               # missing -> default
        dv = prm.default_value()
        if prm.type == "select":
            assert dv in {o[0] for o in prm.options}
        if prm.type in ("int", "number") and dv is not None:
            assert (prm.min is None or dv >= prm.min) and (prm.max is None or dv <= prm.max)
    json.dumps(js, allow_nan=False)


NON_EMPTY = {"unusual-option-volume", "iv-rankings", "option-volume-leaders", "oi-changes", "option-screener",
             "options-broad-view", "put-protection", "covered-calls", "naked-puts", "bull-put-spreads",
             "iron-condors-short", "calendar-calls", "atm-straddles", "earnings-calendar", "earnings-pattern-screener",
             "earnings-upcoming-summary", "term-structure-screener", "skew-screener", "iv-hv-premium",
             "stock-screener", "etf-screener", "market-movers", "performance-by-sector", "seasonality-screener",
             "premarket-movers", "event-driven", "dividend-calendar", "future-ex-dates", "dividend-increases",
             "best-dividends", "dividend-capture", "reits-mlps", "iv-movers"}


@pytest.mark.parametrize("rid", sorted(registry.REPORTS))
def test_report_runs_with_defaults(api, rid):
    r = api.get(f"/api/reports/{rid}")
    assert r.status_code == 200, r.text
    body = r.json()
    keys = {c["key"] for c in body["columns"]}
    for row in body["rows"]:
        assert keys <= set(row), keys - set(row)
    if rid in NON_EMPTY:
        assert body["rows"], f"{rid} returned no rows on the seeded DB"
    assert body["params"].keys() == {p.key for p in registry.REPORTS[rid].params}


def test_unknown_report_404_and_bad_params_422(api):
    assert api.get("/api/reports/nope").status_code == 404
    assert api.get("/api/reports/covered-calls?dte_min=abc").status_code == 422
    assert api.get("/api/reports/covered-calls?pricing=best").status_code == 422
    assert api.get("/api/reports/unusual-option-volume?lookback_days=11").status_code == 422
    assert api.get("/api/reports/covered-calls?watchlist=999").status_code == 422
    assert api.get("/api/reports/earnings-calendar?start=2026-13-01").status_code == 422


def test_param_parsing_and_effective_params(api, seeded):
    r = api.get("/api/reports/option-screener?symbols=aapl,%5EXYZ&right=C&dte_min=0&dte_max=400&delta_min=0.3"
                "&min_oi=0&max_spread_pct=1").json()
    assert r["params"]["symbols"] == ["AAPL", "XYZ"] and r["params"]["right"] == "C"
    assert r["params"]["delta_min"] == 0.3 and r["params"]["dte_max"] == 400
    assert r["rows"] and all(x["right"] == "C" and abs(x["delta"]) >= 0.3 for x in r["rows"])
    assert {x["symbol"] for x in r["rows"]} <= {"AAPL", "XYZ"}
    w = api.get(f"/api/reports/option-screener?watchlist={seeded['watchlist_id']}&symbols=AAPL&min_oi=0").json()
    assert {x["symbol"] for x in w["rows"]} == {"AAPL"}                 # symbols ∩ watchlist
    assert w["params"]["watchlist"] == seeded["watchlist_id"]


def test_sorted_server_side_and_truncated(api):
    r = api.get("/api/reports/option-screener?min_oi=0&dte_min=0&dte_max=900&max_spread_pct=&min_bid=0").json()
    vols = [x["volume"] for x in r["rows"]]
    assert vols == sorted(vols, reverse=True)
    assert len(r["rows"]) <= 5000
    big = api.get("/api/reports/bull-call-spreads?min_oi=0&dte_min=0&dte_max=900&delta_min=0&delta_max=1"
                  "&max_width_steps=20&width_max_pct=1&max_spread_pct=10").json()
    assert big["truncated"] is True and len(big["rows"]) == 5000


def test_param_validation_unit():
    d = registry.REPORTS["covered-calls"]
    with pytest.raises(ParamError):
        registry.parse_params(d, {"dte_min": "-1"})
    p = registry.parse_params(d, {"dte_min": "10", "pricing": "mid", "symbols": "spy, qqq"})
    assert p["dte_min"] == 10 and p["pricing"] == "mid" and p.scope == ["SPY", "QQQ"]
    b = registry.REPORTS["term-structure-screener"]
    assert registry.parse_params(b, {"backwardation_only": "true"})["backwardation_only"] is True
    with pytest.raises(ParamError):
        registry.parse_params(b, {"backwardation_only": "maybe"})


def test_empty_reason_and_method_text():
    """An empty result says why: the run's own notes, else the report's reason, else (chain-based) no chain."""
    from optionlens.services.reports.base import Output
    from optionlens.services.reports.columns import NOTE_CHAIN_UNIVERSE
    d = registry.REPORTS["put-protection"]
    assert NOTE_CHAIN_UNIVERSE in d.notes and d.method
    assert registry.empty_reason(d, Output([])) == registry.NO_CHAIN_MATCH
    assert registry.empty_reason(d, Output([], ["Nothing yet."])) == "Nothing yet."
    assert registry.empty_reason(registry.REPORTS["market-movers"], Output([])) is None
    res = registry.finalize(d, registry.parse_params(d, {}), Output([]))
    assert res["empty_reason"] == registry.NO_CHAIN_MATCH and res["method"] == list(d.method)
    assert "empty_reason" not in registry.finalize(d, registry.parse_params(d, {}), Output([{"symbol": "AAPL"}]))


def test_titles_are_sentence_case_and_unique():
    titles = [d.title for d in registry.REPORTS.values()]
    assert len(titles) == len(set(titles))
    for t in titles:
        rest = t.split(" ")[1:]
        assert all(not (w[:1].isupper() and w[1:].islower()) or w in ("SPY",) for w in rest), t
