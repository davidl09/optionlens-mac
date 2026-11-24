"""The MCP server's tools over a real engine app (in process, through the MCP key): typed calls, shaped output, the
current view round trip (the renderer's report -> the page's data as the user sees it) and the agent-facing errors."""
from dataclasses import replace

import httpx
import pytest

from optionlens.api.app import create_app
from optionlens.config import get_settings
from optionlens.mcp import catalog, connection
from optionlens.mcp.connection import Engine, EngineError
from optionlens.mcp.server import OptionLensTools
from optionlens.services import mcp_access, view

TOKEN = "app-token-0123456789abcdef"


@pytest.fixture
def app(client):
    return create_app(replace(get_settings(), api_token=TOKEN))


@pytest.fixture
async def tools(app):
    """Agent access on; the MCP server's tools reach the app over an in-process loopback transport."""
    access = mcp_access.instance()
    access.enable()
    access.listening(8473)
    engine = Engine(get_settings().data_dir, transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)))
    yield OptionLensTools(engine, catalog.build(app.openapi()))
    await engine.aclose()
    access.disable()
    view.clear()


async def put_view(app, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/api/view", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200, r.text


async def test_symbol_summary(tools):
    s = await tools.call("symbol_summary", {"sym": "AAPL"})
    assert s["symbol"] == "AAPL" and s["name"] == "Apple Inc."


async def test_report_run_with_params_columns_and_paging(tools):
    full = await tools.call("report_run", {"report_id": "large-volume-contracts", "params": {"min_volume": 1},
                                           "limit": 1000})
    total = len(full["rows"])
    assert 100 < total < 1000 and full["params"]["min_volume"] == 1
    first = await tools.call("report_run", {"report_id": "large-volume-contracts", "params": {"min_volume": 1}})
    assert len(first["rows"]) == 100 and first["_page"]["rows"]["total"] == total      # default: 100 rows
    page = await tools.call("report_run", {"report_id": "large-volume-contracts", "params": {"min_volume": 1},
                                           "columns": ["osi", "volume"], "limit": 10, "offset": 10})
    assert len(page["rows"]) == 10 and set(page["rows"][0]) == {"osi", "volume"}
    assert [c["key"] for c in page["columns"]] == ["osi", "volume"]
    assert page["rows"][0] == {"osi": full["rows"][10]["osi"], "volume": full["rows"][10]["volume"]}
    assert page["_page"]["rows"] == {"total": total, "offset": 10, "returned": 10}


async def test_bad_arguments_are_explained(tools):
    with pytest.raises(ValueError, match="report_id"):
        await tools.call("report_run", {"report_id": "no-such-report"})
    with pytest.raises(ValueError, match="Additional properties"):
        await tools.call("symbol_summary", {"sym": "AAPL", "bogus": 1})
    with pytest.raises(EngineError, match="HTTP 422"):
        await tools.call("report_run", {"report_id": "large-volume-contracts", "params": {"min_volume": "lots"}})


async def test_report_list_is_compact(tools):
    lst = await tools.call("report_list", {"category": "Options"})
    assert lst and all(set(d) == {"id", "title", "category", "description", "params"} for d in lst)
    one = await tools.call("report_list", {"report_id": "large-volume-contracts"})
    assert {p["key"] for p in one["params"]} >= {"min_volume"} and one["columns"]


async def test_a_write_and_read_back(tools):
    wl = await tools.call("watchlist_create", {"name": "agent", "symbols": ["AAPL"]})
    await tools.call("watchlist_add_symbol", {"wid": wl["id"], "sym": "SPY"})
    assert (await tools.call("watchlist_get", {"wid": wl["id"]}))["symbols"] == ["AAPL", "SPY"]
    await tools.call("watchlist_delete", {"wid": wl["id"]})


async def test_current_view_returns_the_view_and_the_rows_on_screen(tools, app):
    assert (await tools.call("get_current_view", {}))["view"] is None
    path = "/api/reports/large-volume-contracts?min_volume=1"
    await put_view(app, {
        "route": "/reports/large-volume-contracts?min_volume=1", "title": "Large volume contracts",
        "report_id": "large-volume-contracts",
        "data": [{"method": "GET", "path": path,
                  "table": {"sort": {"key": "volume", "dir": "asc"}, "filters": {"right": {"values": ["C"]}},
                            "hidden": ["oi"]},
                  "rows": {"offset": 2, "limit": 5, "total": 20}},
                 {"method": "GET", "path": "/api/symbol/AAPL/summary"}]})
    got = await tools.call("get_current_view", {})
    assert got["view"]["report_id"] == "large-volume-contracts" and got["view"]["route"].startswith("/reports/")
    report, summary = got["data"]
    assert report["tool"] == "report_run"
    assert report["arguments"] == {"report_id": "large-volume-contracts", "params": {"min_volume": "1"}}
    assert summary["tool"] == "symbol_summary" and summary["arguments"] == {"sym": "AAPL"}
    full = await tools.call("report_run", {"report_id": "large-volume-contracts", "params": {"min_volume": 1},
                                           "limit": 1000})
    calls = sorted((r for r in full["rows"] if r["right"] == "C"), key=lambda r: r["volume"])
    rows = report["result"]["rows"]
    assert [r["osi"] for r in rows] == [r["osi"] for r in calls[2:7]]        # the user's filter, sort, visible rows
    assert "oi" not in rows[0] and "volume" in rows[0]                       # hidden columns stay hidden
    assert report["result"]["_page"]["rows"]["offset"] == 2
    more = await tools.call("get_current_view", {"offset": 0, "limit": 50, "columns": ["osi", "oi"]})
    assert len(more["data"][0]["result"]["rows"]) == min(50, len(calls)) and set(more["data"][0]["result"]["rows"][0]) == {"osi", "oi"}
    assert summary["result"]["symbol"] == "AAPL"
    mime, text = await tools.read("optionlens://current-view")
    assert mime == "application/json" and '"report_id":"large-volume-contracts"' in text


async def test_current_view_never_replays_a_write(tools, app):
    await put_view(app, {"route": "/watchlists", "data": [{"method": "POST", "path": "/api/watchlists",
                                                          "body": {"name": "x"}}]})
    got = await tools.call("get_current_view", {})
    assert got["data"] == [{"request": "POST /api/watchlists", "error": "not available to AI agents"}]


async def test_resources(tools):
    assert (await tools.read("optionlens://conventions"))[1].startswith("# OptionLens data conventions")
    assert '"symbol":"AAPL"' in (await tools.read("optionlens://symbol/AAPL/summary"))[1]
    assert '"universe_size"' in (await tools.read("optionlens://status"))[1]
    with pytest.raises(ValueError):
        await tools.read("optionlens://nope")


async def test_describe_units(tools):
    text = await tools.call("describe_units", {})
    assert "fractions" in text.lower() and "per share" in text
    assert "describe_units" in [t.name for t in tools.listed()]


async def test_agent_facing_errors(tmp_path):
    engine = Engine(tmp_path)
    with pytest.raises(EngineError) as e:
        await engine.call("GET", "/api/status")
    assert "Settings → AI agents (MCP)" in str(e.value)                   # access off: no connection file
    (tmp_path / "mcp-connection.json").write_text('{"port": 9, "mcp_token": "%s"}' % ("k" * 43))
    with pytest.raises(EngineError) as e:
        await engine.call("GET", "/api/status")
    assert str(e.value) == connection.NOT_RUNNING and "k" * 43 not in str(e.value)
    await engine.aclose()


def test_app_data_dir_per_os(tmp_path):
    assert connection.app_data_dir("darwin", {}, tmp_path) == tmp_path / "Library/Application Support/OptionLens/data"
    assert connection.app_data_dir("win32", {"APPDATA": str(tmp_path / "Roaming")}, tmp_path) == \
        tmp_path / "Roaming" / "OptionLens" / "data"
    assert connection.app_data_dir("linux", {}, tmp_path) == tmp_path / ".config/OptionLens/data"
    assert connection.app_data_dir("linux", {"XDG_CONFIG_HOME": str(tmp_path / "x")}, tmp_path) == \
        tmp_path / "x" / "OptionLens" / "data"
