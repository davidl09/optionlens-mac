"""End to end, as an agent uses it: a real engine process (seeded database, bearer token, random port), agent access
turned on through the API like Settings does, and the MCP SDK's own client talking to `python -m optionlens.mcp` over
stdio. Runs on Linux, macOS and Windows (CI: the engine matrix)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from mcp import Client, StdioServerParameters

ENGINE_DIR = Path(__file__).resolve().parents[2]
TOKEN = "e2e-app-token-0123456789abcdef"
SEED = """
import sys
from pathlib import Path
from optionlens import db
from tests.api.conftest import seed
db.connect(Path(sys.argv[1]) / "optionlens.duckdb")
seed()
db.close()
"""


def _env(data_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPTIONLENS_")}
    return {**env, "OPTIONLENS_DATA_DIR": str(data_dir), "OPTIONLENS_NO_SCHEDULER": "1",
            "OPTIONLENS_CHAIN_SOURCES": "ibkr-bridge", "PYTHONUNBUFFERED": "1",
            # No data source on the internet: anything the engine would fetch fails fast.
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost"}


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    data = tmp_path_factory.mktemp("e2e-data")
    subprocess.run([sys.executable, "-c", SEED, str(data)], cwd=ENGINE_DIR, env=_env(data), check=True, timeout=120)
    proc = subprocess.Popen([sys.executable, "-m", "optionlens.cli", "serve", "--port", "0", "--exit-on-stdin-eof"],
                            cwd=ENGINE_DIR, env={**_env(data), "OPTIONLENS_API_TOKEN": TOKEN},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    port: list[int] = []
    ready = threading.Event()

    def read() -> None:
        for line in proc.stdout:
            if line.startswith("LISTENING ") and not port:
                port.append(int(line.split()[1]))
                ready.set()
        ready.set()

    threading.Thread(target=read, daemon=True).start()
    assert ready.wait(120) and port, "the engine did not start"
    base = f"http://127.0.0.1:{port[0]}"
    yield {"base": base, "data": data, "app": {"Authorization": f"Bearer {TOKEN}"}}
    proc.stdin.close()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()


def server(data: Path) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPTIONLENS_")}
    return StdioServerParameters(command=sys.executable, args=["-m", "optionlens.mcp", "--data-dir", str(data)],
                                 env=env, cwd=str(ENGINE_DIR))


def payload(result) -> object:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def test_an_agent_over_stdio(engine):
    async with httpx.AsyncClient(base_url=engine["base"], headers=engine["app"], timeout=30) as app:
        r = await app.put("/api/mcp", json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"]
        conn = json.loads((engine["data"] / "mcp-connection.json").read_text())
        assert conn["port"] == int(engine["base"].rsplit(":", 1)[1])          # the running engine's port
        await app.put("/api/view", json={
            "route": "/s/AAPL/summary", "symbol": "AAPL", "tab": "summary",
            "data": [{"method": "GET", "path": "/api/symbol/AAPL/summary"}]})

        async with Client(server(engine["data"]), read_timeout_seconds=120) as agent:
            names = {t.name for t in (await agent.list_tools()).tools}
            assert {"symbol_summary", "symbol_chain", "report_run", "report_list", "get_current_view",
                    "watchlist_create", "describe_units"} <= names
            assert not names & {"ingest_chain", "ingest_wants", "mcp", "view", "health", "live_stream"}
            tools = {t.name: t for t in (await agent.list_tools()).tools}
            assert tools["watchlist_delete"].annotations.destructive_hint is True
            assert tools["symbol_chain"].annotations.read_only_hint is True

            s = payload(await agent.call_tool("symbol_summary", {"sym": "AAPL"}))
            assert s["symbol"] == "AAPL"

            rep = payload(await agent.call_tool("report_run", {"report_id": "large-volume-contracts",
                                                               "params": {"min_volume": 1, "right": "C"},
                                                               "columns": ["osi", "right", "volume"], "limit": 5}))
            assert len(rep["rows"]) == 5 and {r["right"] for r in rep["rows"]} == {"C"}
            assert set(rep["rows"][0]) == {"osi", "right", "volume"} and rep["params"]["right"] == "C"

            v = payload(await agent.call_tool("get_current_view", {}))
            assert v["view"]["symbol"] == "AAPL" and v["data"][0]["result"]["symbol"] == "AAPL"

            wl = payload(await agent.call_tool("watchlist_create", {"name": "From the agent", "symbols": ["AAPL"]}))
            payload(await agent.call_tool("watchlist_add_symbol", {"wid": wl["id"], "sym": "SPY"}))
            assert payload(await agent.call_tool("watchlist_get", {"wid": wl["id"]}))["symbols"] == ["AAPL", "SPY"]
            back = (await app.get(f"/api/watchlists/{wl['id']}")).json()          # what the app shows
            assert back["name"] == "From the agent" and back["symbols"] == ["AAPL", "SPY"]

            res = await agent.read_resource("optionlens://status")
            assert '"universe_size"' in res.contents[0].text

            seen = (await app.get("/api/mcp")).json()
            assert seen["last_call"] and seen["clients"]

            assert (await app.put("/api/mcp", json={"enabled": False})).json()["enabled"] is False
            off = await agent.call_tool("symbol_summary", {"sym": "AAPL"})
            assert off.is_error and "access is off" in off.content[0].text
