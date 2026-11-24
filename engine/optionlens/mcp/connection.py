"""The running OptionLens engine as the MCP server reaches it (docs/INTEGRATION.md §10): the connection file the app
writes while agent access is on ({port, mcp_token} in the app's data folder), and HTTP calls to the engine on loopback
with that key. The file is read again for every call, so a new port (app restart) or a regenerated key is picked up
without restarting the MCP server. The key is never logged or put in an error message."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

import httpx

from ..fileio import read_json
from ..services.mcp_access import FILE_NAME

APP_DIR_NAME = "OptionLens"
TIMEOUT_S = 120.0            # reports and backtests can take a while on a large universe
CLIENT_HEADER = "X-OptionLens-MCP-Client"

OFF = ("OptionLens AI-agent access is off. Turn it on in the OptionLens app: Settings → AI agents (MCP) → "
       "\"Allow AI agents\".")
NOT_RUNNING = "OptionLens is not running. Start the OptionLens app (agent access stays on), then try again."
REJECTED = ("OptionLens rejected the MCP key: it was regenerated or agent access was turned off. Turn it on in "
            "Settings → AI agents (MCP); this MCP server reads the new key by itself.")


class EngineError(Exception):
    """A call that did not succeed, with a message for the agent (never the key)."""


def app_data_dir(platform: str = sys.platform, env: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """The app's engine data folder (Electron userData/data): macOS ~/Library/Application Support/OptionLens/data,
    Windows %APPDATA%\\OptionLens\\data, Linux $XDG_CONFIG_HOME (~/.config)/OptionLens/data."""
    home = home or Path.home()
    if platform == "darwin":
        base = home / "Library" / "Application Support"
    elif platform == "win32":
        base = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
    else:
        base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    return base / APP_DIR_NAME / "data"


def connection_file(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


class Engine:
    """Authenticated calls to the running engine."""

    def __init__(self, data_dir: Path, timeout_s: float = TIMEOUT_S, transport: httpx.AsyncBaseTransport | None = None):
        self.path = connection_file(data_dir)
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport, trust_env=False)

    def _connection(self) -> tuple[str, str]:
        raw = read_json(self.path)
        if raw is None:
            raise EngineError(OFF)
        port, token = (raw.get("port"), raw.get("mcp_token")) if isinstance(raw, dict) else (None, None)
        if not isinstance(token, str) or not token:
            raise EngineError(OFF)
        if not isinstance(port, int) or not 0 < port < 65536:
            raise EngineError(NOT_RUNNING)
        return f"http://127.0.0.1:{port}", token

    async def call(self, method: str, path: str, query: Mapping[str, str] | None = None, body: Any = None,
                   client: str | None = None) -> Any:
        """The JSON answer of `method path` (path may carry its own query string). EngineError otherwise."""
        base, token = self._connection()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if client:
            headers[CLIENT_HEADER] = client[:64]
        try:
            r = await self._client.request(method, base + path, params=dict(query or {}) or None, headers=headers,
                                           json=body if method != "GET" else None)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise EngineError(NOT_RUNNING) from None
        except httpx.TimeoutException:
            raise EngineError(f"OptionLens did not answer within {self._client.timeout.read:.0f} s "
                              "(a slow data source?). Try again.") from None
        except httpx.HTTPError as e:
            raise EngineError(f"Could not reach OptionLens: {type(e).__name__}") from None
        if r.status_code == 401:
            raise EngineError(REJECTED)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            code = data.get("code") if isinstance(data, dict) else None
            raise EngineError(f"HTTP {r.status_code}: {detail or r.reason_phrase}" + (f" (code {code})" if code else ""))
        return data

    async def aclose(self) -> None:
        await self._client.aclose()
