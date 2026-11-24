"""AI-agent (MCP) access to the engine (docs/INTEGRATION.md §10).

Off by default. Enabled = a long-lived random MCP key exists. Its single home is the connection file
`DATA_DIR/mcp-connection.json` = {"port", "mcp_token"} (mode 0600; Windows: the user profile's default ACL), which
the stdio MCP server (`python -m optionlens.mcp`) reads to reach the running engine. The engine rewrites the port on
every start (it binds a random port). Turning access off removes the file and revokes the key at once; turning it on
again, or "Regenerate", makes a new key. The key is never logged or returned by the API.

Which requests the key may make is decided by the engine's auth (api/auth.py) from the MCP tool catalog."""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..fileio import read_json, write_json_atomic

FILE_NAME = "mcp-connection.json"
CLIENT_WINDOW_S = 600          # a client that called within this window counts as connected
MAX_CLIENTS = 20
_CLIENT_NAME = re.compile(r"[^A-Za-z0-9 ._/()+-]")
log = logging.getLogger("optionlens.mcp_access")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def client_label(raw: str | None) -> str:
    """A client name header value as shown in Settings: printable, at most 64 characters, "unknown" when absent."""
    s = _CLIENT_NAME.sub("", (raw or "").strip())[:64].strip()
    return s or "unknown"


class McpAccess:
    """The MCP key, the connection file and what agents did recently (thread-safe)."""

    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        self._lock = threading.Lock()
        self._port: int | None = None
        self._last_call: dict | None = None
        self._clients: dict[str, float] = {}
        self._token = self._load()

    def _load(self) -> str | None:
        raw = read_json(self.path)
        token = raw.get("mcp_token") if isinstance(raw, dict) else None
        if raw is not None and not (isinstance(token, str) and len(token) >= 32):
            log.warning("ignoring an unreadable MCP connection file (%s): MCP access is off", self.path)
            return None
        return token

    # ------------------------------------------------------------------ state
    def token(self) -> str | None:
        return self._token

    @property
    def enabled(self) -> bool:
        return self._token is not None

    def listening(self, port: int) -> None:
        """The engine bound `port`: an enabled connection file gets it (the port changes on every start)."""
        with self._lock:
            self._port = port
            if self._token is not None:
                self._write()

    def enable(self) -> dict:
        """On (a new key when it was off; an enabled key is kept)."""
        with self._lock:
            if self._token is None:
                self._token = secrets.token_urlsafe(32)
                self._write()
                log.info("MCP access enabled")
        return self.status()

    def disable(self) -> dict:
        """Off: the key is revoked at once and the connection file removed."""
        with self._lock:
            self._token = None
            self._clients.clear()
            self.path.unlink(missing_ok=True)
            log.info("MCP access disabled")
        return self.status()

    def regenerate(self) -> dict:
        """A new key (the old one stops working at once). ValueError while access is off."""
        with self._lock:
            if self._token is None:
                raise ValueError("MCP access is off")
            self._token = secrets.token_urlsafe(32)
            self._clients.clear()
            self._write()
            log.info("MCP key regenerated")
        return self.status()

    def _write(self) -> None:
        write_json_atomic(self.path, {"port": self._port, "mcp_token": self._token}, mode=0o600)

    # ------------------------------------------------------------------ activity
    def record(self, client: str | None, tool_path: str) -> None:
        """An agent request the key authorized (the operation's path template, never its arguments)."""
        now = self.clock()
        name = client_label(client)
        with self._lock:
            self._last_call = {"at": _iso(now), "operation": tool_path, "client": name}
            self._clients[name] = now
            if len(self._clients) > MAX_CLIENTS:
                del self._clients[min(self._clients, key=self._clients.__getitem__)]

    def status(self) -> dict:
        """GET /api/mcp: {enabled, connection_file, port, last_call, clients[{name, last_seen_at}]} (no key)."""
        now = self.clock()
        with self._lock:
            recent = sorted(((n, t) for n, t in self._clients.items() if now - t <= CLIENT_WINDOW_S),
                            key=lambda x: -x[1])
            return {"enabled": self._token is not None, "connection_file": str(self.path), "port": self._port,
                    "last_call": self._last_call if self._token is not None else None,
                    "clients": [{"name": n, "last_seen_at": _iso(t)} for n, t in recent]}


_instance: McpAccess | None = None


def instance() -> McpAccess:
    """The process-wide McpAccess of the configured DATA_DIR."""
    global _instance
    from ..config import get_settings
    path = get_settings().data_dir / FILE_NAME
    if _instance is None or _instance.path != path:
        _instance = McpAccess(path)
    return _instance
