"""AI-agent (MCP) access: the key's lifecycle (on / off / regenerate), the connection file, what the key may call
(loopback only, no web pages, only the MCP catalog's operations), the activity Settings shows, and the current view."""
import os
import stat
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from optionlens.api.app import create_app
from optionlens.config import get_settings
from optionlens.fileio import read_json
from optionlens.services import mcp_access, view

TOKEN = "app-token-0123456789abcdef"
LOCAL = ("127.0.0.1", 50123)


@pytest.fixture
def app_client(client):
    """App with the per-launch token, as a loopback client; shares the session client's database."""
    app = create_app(replace(get_settings(), api_token=TOKEN))
    yield TestClient(app, client=LOCAL)
    mcp_access.instance().disable()
    view.clear()


def as_app(**kw):
    return {"Authorization": f"Bearer {TOKEN}", **kw}


def enable(c) -> str:
    r = c.put("/api/mcp", json={"enabled": True}, headers=as_app())
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert "mcp_token" not in r.text
    return read_json(mcp_access.instance().path)["mcp_token"]


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def test_off_by_default_and_the_key_is_refused(app_client):
    s = app_client.get("/api/mcp", headers=as_app()).json()
    assert s["enabled"] is False and s["last_call"] is None and s["clients"] == []
    assert not mcp_access.instance().path.exists()
    assert app_client.get("/api/status", headers=bearer("x" * 43)).status_code == 401


def test_enable_writes_a_private_connection_file(app_client):
    key = enable(app_client)
    path = mcp_access.instance().path
    assert path.name == "mcp-connection.json" and path.parent == get_settings().data_dir
    assert set(read_json(path)) == {"port", "mcp_token"} and len(key) >= 32
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    mcp_access.instance().listening(54321)            # the engine bound its port: the file says so
    assert read_json(path) == {"port": 54321, "mcp_token": key}
    assert app_client.put("/api/mcp", json={"enabled": True}, headers=as_app()).status_code == 200
    assert read_json(path)["mcp_token"] == key           # turning on again keeps the key


def test_the_key_reads_what_agents_may_read(app_client):
    key = enable(app_client)
    for path in ("/api/status", "/api/symbol/AAPL/summary", "/api/reports", "/api/watchlists", "/api/live/quotes",
                 "/api/view"):
        assert app_client.get(path, headers=bearer(key)).status_code == 200, path
    r = app_client.post("/api/watchlists", json={"name": "agent list", "symbols": ["AAPL"]}, headers=bearer(key))
    assert r.status_code == 200
    wid = r.json()["id"]
    assert app_client.get(f"/api/watchlists/{wid}", headers=bearer(key)).json()["symbols"] == ["AAPL"]
    assert app_client.delete(f"/api/watchlists/{wid}", headers=bearer(key)).status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/ingest/heartbeat", {"client": "x", "ibkr_connected": True}),
    ("GET", "/api/ingest/wants", None),
    ("POST", "/api/sources/questrade/token", {"refresh_token": "abc"}),
    ("DELETE", "/api/sources/questrade/token", None),
    ("PUT", "/api/live/mode", {"mode": "stream"}),
    ("POST", "/api/live/subscribe", {"client_id": "abcdefgh12"}),
    ("GET", "/api/mcp", None),
    ("PUT", "/api/mcp", {"enabled": False}),
    ("POST", "/api/mcp/regenerate", None),
    ("PUT", "/api/view", {"route": "/"}),
    ("POST", "/api/symbol/AAPL/refresh", None),
])
def test_the_key_never_reaches_internal_endpoints(app_client, method, path, body):
    key = enable(app_client)
    r = app_client.request(method, path, json=body, headers=bearer(key))
    assert r.status_code == 403 and r.json()["code"] == "mcp_forbidden", (method, path, r.text)
    assert mcp_access.instance().enabled                # the key cannot turn agent access off (or on) itself


def test_the_key_is_accepted_from_loopback_only_and_never_from_web_pages(app_client):
    key = enable(app_client)
    remote = TestClient(app_client.app, client=("192.168.1.20", 50000))
    assert remote.get("/api/status", headers=bearer(key)).status_code == 403
    assert TestClient(app_client.app, client=("::1", 50000)).get("/api/status", headers=bearer(key)).status_code == 200
    r = app_client.get("/api/status", headers={**bearer(key), "Origin": "app://optionlens"})
    assert r.status_code == 403 and "web pages" in r.json()["detail"]
    assert remote.get("/api/status", headers=as_app()).status_code == 200    # the app's own token: unchanged


def test_off_revokes_at_once_and_removes_the_file(app_client):
    key = enable(app_client)
    assert app_client.put("/api/mcp", json={"enabled": False}, headers=as_app()).json()["enabled"] is False
    assert not mcp_access.instance().path.exists()
    assert app_client.get("/api/status", headers=bearer(key)).status_code == 401
    assert enable(app_client) != key                    # on again: a new key


def test_regenerate_rotates_the_key(app_client):
    assert app_client.post("/api/mcp/regenerate", headers=as_app()).status_code == 409     # off: nothing to rotate
    old = enable(app_client)
    assert app_client.post("/api/mcp/regenerate", headers=as_app()).status_code == 200
    new = read_json(mcp_access.instance().path)["mcp_token"]
    assert new != old
    assert app_client.get("/api/status", headers=bearer(old)).status_code == 401
    assert app_client.get("/api/status", headers=bearer(new)).status_code == 200


def test_settings_see_the_last_call_and_connected_clients_never_the_key(app_client):
    key = enable(app_client)
    app_client.get("/api/symbol/AAPL/summary", headers={**bearer(key), "X-OptionLens-MCP-Client": "claude-code/2.1\n"})
    s = app_client.get("/api/mcp", headers=as_app()).json()
    assert s["last_call"]["operation"] == "GET /api/symbol/{sym}/summary"      # the operation, not its arguments
    assert s["last_call"]["client"] == "claude-code/2.1" and [c["name"] for c in s["clients"]] == ["claude-code/2.1"]
    assert key not in str(s)


def test_a_corrupt_connection_file_means_off(tmp_path):
    p = tmp_path / "mcp-connection.json"
    p.write_text("{not json")
    assert mcp_access.McpAccess(p).enabled is False
    p.write_text('{"port": 1, "mcp_token": "short"}')
    assert mcp_access.McpAccess(p).enabled is False


def test_the_key_survives_an_engine_restart(tmp_path):
    a = mcp_access.McpAccess(tmp_path / "mcp-connection.json")
    a.enable()
    key = a.token()
    b = mcp_access.McpAccess(tmp_path / "mcp-connection.json")     # the next engine start
    b.listening(40000)
    assert b.token() == key and read_json(b.path) == {"port": 40000, "mcp_token": key}


# ----------------------------------------------------------------------------- the current view
VIEW = {"route": "/reports/iv-rankings?min_price=5", "title": "IV rankings", "report_id": "iv-rankings",
        "details": {"category": "Volatility"},
        "data": [{"method": "GET", "path": "/api/reports/iv-rankings?min_price=5",
                  "table": {"sort": {"key": "iv30", "dir": "desc"}, "filters": {"iv30": {"min": 0.2}},
                            "hidden": ["sector"]},
                  "rows": {"offset": 0, "limit": 50, "total": 120}}]}


def test_the_renderer_reports_its_view_and_agents_read_it(app_client):
    assert app_client.get("/api/view", headers=as_app()).json() == {"view": None, "reported_at": None, "age_s": None}
    assert app_client.put("/api/view", json=VIEW, headers=as_app()).json() == {"ok": True}
    key = enable(app_client)
    got = app_client.get("/api/view", headers=bearer(key)).json()
    assert got["view"]["route"] == VIEW["route"] and got["view"]["data"][0]["table"]["filters"]["iv30"]["min"] == 0.2
    assert got["reported_at"] and got["age_s"] >= 0


@pytest.mark.parametrize("bad", [{"route": "/", "data": [{"method": "DELETE", "path": "/api/x"}]},
                                 {"route": "/", "data": [{"method": "GET", "path": "http://evil/api"}]},
                                 {"route": "/", "extra": 1}])
def test_a_malformed_view_is_rejected(app_client, bad):
    assert app_client.put("/api/view", json=bad, headers=as_app()).status_code == 422


def test_live_quotes_snapshot_endpoint(app_client):
    r = app_client.get("/api/live/quotes?symbols=aapl,SPY", headers=as_app())
    assert r.status_code == 200 and set(r.json()) == {"status", "quotes", "options"}
    assert app_client.get("/api/live/quotes?symbols=$$$", headers=as_app()).status_code == 422
