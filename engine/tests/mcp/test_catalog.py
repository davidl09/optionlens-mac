"""The MCP tool catalog is generated from the engine's OpenAPI schema: every GET under /api is a tool or explicitly
excluded, writes are tools only when curated, excluded operations never appear, schemas are valid JSON Schema."""
import re

import jsonschema
import pytest

from optionlens.api.app import create_app
from optionlens.mcp import catalog


@pytest.fixture(scope="module")
def openapi():
    return create_app().openapi()


@pytest.fixture(scope="module")
def tools(openapi):
    return catalog.build(openapi)


def operations(openapi):
    return [f"{m.upper()} {p}" for p, ops in openapi["paths"].items() if p.startswith("/api/") for m in ops]


def test_every_get_is_a_tool_unless_excluded(openapi, tools):
    exposed = {t.operation for t in tools}
    for op in operations(openapi):
        if op.startswith("GET "):
            assert (op in exposed) != bool(catalog.excluded(op)), op


def test_every_other_operation_is_curated_or_excluded(openapi, tools):
    """A new write endpoint needs a decision: curated (with annotations) or excluded with a reason."""
    exposed = {t.operation for t in tools}
    for op in operations(openapi):
        if not op.startswith("GET "):
            assert op in exposed or catalog.excluded(op), f"{op}: add it to CURATED or EXCLUDED"
            assert not (op in exposed and catalog.excluded(op)), op


def test_curated_and_excluded_entries_name_real_operations(openapi):
    ops = set(operations(openapi))
    assert set(catalog.CURATED) <= ops, set(catalog.CURATED) - ops
    assert set(catalog.EXCLUDED) <= ops, set(catalog.EXCLUDED) - ops


EXCLUDED_NOW = [
    "GET /api/health", "POST /api/symbol/{sym}/refresh", "POST /api/sources/questrade/token",
    "DELETE /api/sources/questrade/token", "POST /api/live/subscribe", "GET /api/live/stream", "PUT /api/live/mode",
    "GET /api/mcp", "PUT /api/mcp", "POST /api/mcp/regenerate", "PUT /api/view",
    "GET /api/ingest/wants", "GET /api/ingest/live-wants", "POST /api/ingest/chain", "POST /api/ingest/heartbeat",
    "POST /api/ingest/refresh-result", "POST /api/ingest/quotes", "POST /api/ingest/dividend-history",
]


@pytest.mark.parametrize("op", EXCLUDED_NOW)
def test_internal_endpoints_are_not_tools(tools, op):
    assert catalog.excluded(op)
    assert op not in {t.operation for t in tools}
    method, path = op.split(" ", 1)
    assert catalog.Matcher(tools).match(method, re.sub(r"\{[^}]+\}", "X", path)) is None


def test_names_are_unique_grouped_snake_case(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    groups = ("symbol_", "report_", "market_", "compare_", "watchlist_", "alert_", "preset_", "notification_",
              "live_", "source_", "calc_", "backtest_", "system_", "portfolio_", "get_current_view")
    for n in names:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", n) and n.startswith(groups), n


def test_the_expected_tools_exist(tools):
    by = {t.name: t for t in tools}
    reads = ["symbol_summary", "symbol_key_stats", "symbol_chain", "symbol_volatility", "symbol_contract", "report_list", "report_run",
             "market_overview", "compare_volatility", "compare_straddle", "system_status", "source_questrade_status",
             "live_quotes", "live_status", "notification_list", "watchlist_list", "watchlist_table", "preset_list",
             "alert_list", "calc_strategy_templates", "calc_strategy", "backtest_stock", "backtest_options",
             "get_current_view"]
    for n in reads:
        assert by[n].read_only and not by[n].destructive, n
    writes = {"watchlist_create": (False, False), "watchlist_update": (True, True), "watchlist_delete": (True, True),
              "watchlist_add_symbol": (False, True), "watchlist_remove_symbol": (True, True),
              "alert_create": (False, False), "alert_update": (True, True), "alert_delete": (True, True),
              "preset_save": (False, False), "preset_delete": (True, True), "notification_mark_read": (False, True)}
    for n, (destructive, idempotent) in writes.items():
        assert not by[n].read_only and (by[n].destructive, by[n].idempotent) == (destructive, idempotent), n
    assert sorted(t.name for t in tools if not t.read_only) == sorted(writes)


def test_input_schemas_are_valid_and_typed(tools):
    for t in tools:
        jsonschema.Draft202012Validator.check_schema(t.input_schema)
        assert t.input_schema["type"] == "object" and t.input_schema["additionalProperties"] is False
        assert '"title"' not in repr(t.input_schema).replace("'", '"'), t.name
        for p in t.path_params:
            assert p in t.input_schema["required"], (t.name, p)
            assert t.input_schema["properties"][p].get("description"), (t.name, p)
        if t.read_only:
            assert {"limit", "offset", "columns"} <= set(t.input_schema["properties"]), t.name


def test_report_run_is_typed_from_the_registry(tools):
    from optionlens.services.reports import registry
    run = next(t for t in tools if t.name == "report_run")
    assert run.input_schema["properties"]["report_id"]["enum"] == list(registry.REPORTS)
    assert run.input_schema["properties"]["params"]["type"] == "object"
    path, query, body = run.request({"report_id": "iv-rankings",
                                     "params": {"symbols": ["AAPL", "MSFT"], "earnings": True, "min_price": 5}})
    assert (path, query, body) == ("/api/reports/iv-rankings",
                                   {"symbols": "AAPL,MSFT", "earnings": "true", "min_price": "5"}, None)


def test_requests_split_path_query_and_body(tools):
    by = {t.name: t for t in tools}
    assert by["symbol_key_stats"].request({"sym": "KO"}) == ("/api/symbol/KO/key-stats", {}, None)
    assert "P/E" in by["symbol_key_stats"].description and "sources" in by["symbol_key_stats"].description
    assert by["symbol_chain"].request({"sym": "AAPL", "expiry": "2026-12-18", "limit": 5}) == (
        "/api/symbol/AAPL/chain", {"expiry": "2026-12-18"}, None)
    assert by["alert_update"].request({"aid": 3, "enabled": False}) == ("/api/alerts/3", {}, {"enabled": False})
    assert by["watchlist_add_symbol"].request({"wid": 1, "sym": "BRK.B"}) == ("/api/watchlists/1/symbols/BRK.B", {}, None)
    assert by["notification_mark_read"].request({}) == ("/api/notifications/read", {}, {})
    calc = by["calc_strategy"]
    assert calc.body_params and "legs" in calc.input_schema["required"] and "$defs" in calc.input_schema
    with pytest.raises(ValueError):
        by["symbol_summary"].request({})
    assert by["symbol_contract"].request({"sym": "AAPL", "osi": "A/B"})[0] == "/api/symbol/AAPL/contract/A%2FB"


def test_matcher_prefers_literal_paths(tools):
    m = catalog.Matcher(tools)
    assert m.match("GET", "/api/watchlists/7/table").name == "watchlist_table"
    assert m.match("GET", "/api/watchlists/7").name == "watchlist_get"
    assert m.match("GET", "/api/reports").name == "report_list"
    assert m.match("GET", "/api/symbol/AAPL/chain/extra") is None


def test_an_uncurated_get_becomes_a_tool_by_itself():
    doc = {"paths": {"/api/new/thing-list": {"get": {"description": "New things.", "parameters": [
        {"name": "n", "in": "query", "schema": {"anyOf": [{"type": "integer"}, {"type": "null"}], "title": "N"}}]}},
        "/api/new/write": {"post": {}}, "/api/ingest/x": {"get": {}}}}
    [t] = catalog.build(doc)
    assert (t.name, t.read_only, t.description) == ("new_thing_list", True, "New things. (GET /api/new/thing-list).")
    assert t.input_schema["properties"]["n"] == {"type": "integer"}
