"""Context-friendly output: pagination, column subsets, the shared table-state rule, the compact report catalog."""
import json
from pathlib import Path

import pytest

from optionlens.mcp import shaping
from optionlens.mcp.catalog import PAGE_DEFAULT, PAGE_MAX

CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "table-state-cases.json").read_text())


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_table_state_matches_the_renderer(case):
    """The same cases as src/lib/table.test.ts (the renderer's applyTable)."""
    assert [r["id"] for r in shaping.apply_table(CASES["rows"], case["state"])] == case["expect"]


def test_watchlist_only_keeps_rows_of_those_symbols():
    rows = shaping.apply_table(CASES["rows"], {"symbols": ["XOM", "AAPL"], "sort": {"key": "id", "dir": "desc"}})
    assert [r["id"] for r in rows] == [3, 1]


def test_long_arrays_are_paginated_with_a_note():
    value = {"calls": list(range(250)), "expiries": list(range(8)), "nested": {"history": list(range(150))}}
    out = shaping.paginate(value, 0, PAGE_DEFAULT)
    assert out["calls"] == list(range(100)) and out["expiries"] == list(range(8))
    assert out["nested"]["history"] == list(range(100))
    assert out["_page"] == {"calls": {"total": 250, "offset": 0, "returned": 100},
                            "nested.history": {"total": 150, "offset": 0, "returned": 100}}


def test_offset_pages_and_negative_offset_counts_from_the_end():
    assert shaping.paginate(list(range(250)), 200, 100) == {
        "items": list(range(200, 250)), "_page": {"items": {"total": 250, "offset": 200, "returned": 50}}}
    assert shaping.paginate(list(range(250)), -10, 100)["items"] == list(range(240, 250))
    assert shaping.paginate(list(range(50)), 0, 100) == list(range(50))       # short: untouched, no note


def test_arrays_inside_arrays_report_the_largest():
    out = shaping.paginate({"rows": [{"legs": list(range(5))}, {"legs": list(range(7))}]}, 0, 3)
    assert out["_page"] == {"rows[].legs": {"total": 7, "offset": 0, "returned": 3}}


def test_columns_narrow_rows_and_report_columns():
    result = {"columns": [{"key": "symbol"}, {"key": "iv"}, {"key": "volume"}],
              "rows": [{"symbol": "A", "iv": 0.2, "volume": 1}], "notes": ["n"]}
    out = shaping.select_columns(result, ["symbol", "iv"])
    assert out == {"columns": [{"key": "symbol"}, {"key": "iv"}], "rows": [{"symbol": "A", "iv": 0.2}], "notes": ["n"]}


def test_page_args_are_bounded():
    assert shaping.page_args({}) == (PAGE_DEFAULT, 0, None)
    assert shaping.page_args({"limit": 10**6, "offset": -5, "columns": ["a"]}) == (PAGE_MAX, -5, ["a"])
    assert shaping.page_args({"limit": 0, "offset": True})[:2] == (1, 0)


def test_shape_selects_then_paginates():
    rows = [{"symbol": f"S{i}", "iv": i} for i in range(150)]
    out = shaping.shape({"columns": [{"key": "symbol"}, {"key": "iv"}], "rows": rows},
                        {"columns": ["symbol"], "limit": 20, "offset": 20})
    assert out["rows"][0] == {"symbol": "S20"} and len(out["rows"]) == 20
    assert out["_page"]["rows"] == {"total": 150, "offset": 20, "returned": 20}


def test_compact_json():
    assert shaping.dumps({"a": [1, 2], "b": "é"}) == '{"a":[1,2],"b":"é"}'


DEFS = [{"id": "iv-rankings", "title": "IV rankings", "category": "Volatility", "description": "d",
         "params": [{"key": "min_price", "type": "number"}], "columns": [{"key": "symbol"}]},
        {"id": "dividend-calendar", "title": "Dividends", "category": "Dividends", "description": "d", "params": [],
         "columns": []}]


def test_report_list_is_compact_and_describes_one_report():
    assert shaping.report_list(DEFS, {}) == [
        {"id": "iv-rankings", "title": "IV rankings", "category": "Volatility", "description": "d", "params": ["min_price"]},
        {"id": "dividend-calendar", "title": "Dividends", "category": "Dividends", "description": "d", "params": []}]
    assert [d["id"] for d in shaping.report_list(DEFS, {"category": "Dividends"})] == ["dividend-calendar"]
    assert shaping.report_list(DEFS, {"report_id": "iv-rankings"}) == DEFS[0]
    with pytest.raises(KeyError):
        shaping.report_list(DEFS, {"report_id": "nope"})
