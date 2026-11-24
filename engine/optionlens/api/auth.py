"""Bearer-token authentication (docs/INTEGRATION.md §2, §10): when the app passed OPTIONLENS_API_TOKEN, every request
except `GET /api/health` must carry `Authorization: Bearer <token>`; comparison is constant-time. CORS preflights are
answered by the CORS middleware, which wraps this one.

The MCP key (AI agents, services/mcp_access.py) is accepted besides the app's token while agent access is on, and
only: from a loopback client, without an `Origin` header (no browser page), for the operations of the MCP tool
catalog (mcp/catalog.py), and a gated tool (the portfolio) only while the user allowed that gate. Anything else with
that key: 403."""
from __future__ import annotations

import hmac
from typing import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from ..mcp import catalog
from ..services.mcp_access import McpAccess
from .json import ApiJSON

OPEN_PATHS = frozenset({("GET", "/api/health"), ("HEAD", "/api/health")})
MCP_CLIENT_HEADER = b"x-optionlens-mcp-client"


def token_ok(header: str | None, token: str) -> bool:
    """Whether an Authorization header value is `Bearer <token>` (constant-time on the token)."""
    scheme, _, given = (header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), token.encode())


def is_loopback(host: str | None) -> bool:
    h = host or ""
    return h == "::1" or h.startswith("127.") or h.startswith("::ffff:127.")


class BearerAuth:
    """ASGI middleware: 401 {"detail": "unauthorized"} unless the request is open or authorized; 403 for an MCP-key
    request outside what agents may do."""

    def __init__(self, app: ASGIApp, token: str, mcp: Callable[[], McpAccess] | None = None):
        self.app, self.token, self.mcp = app, token, mcp
        self._matcher: catalog.Matcher | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (scope["method"], scope["path"]) not in OPEN_PATHS:
            headers = dict(scope["headers"])
            header = headers[b"authorization"].decode("latin-1") if b"authorization" in headers else None
            if not token_ok(header, self.token):
                gate = self.mcp() if self.mcp is not None else None
                key = gate.token() if gate is not None else None
                if gate is None or key is None or not token_ok(header, key):
                    resp = ApiJSON({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                    await resp(scope, receive, send)
                    return
                tool, denied = self._mcp_check(scope, headers)
                if tool is None:
                    await ApiJSON({"detail": denied, "code": "mcp_forbidden"}, status_code=403)(scope, receive, send)
                    return
                gate.record(headers.get(MCP_CLIENT_HEADER, b"").decode("latin-1"), tool.operation)
        await self.app(scope, receive, send)

    def _mcp_check(self, scope: Scope, headers: dict[bytes, bytes]) -> tuple[catalog.ToolDef | None, str]:
        """The tool an MCP-key request belongs to, or None and why the key may not make it."""
        client = scope.get("client")
        if not is_loopback(client[0] if client else None):
            return None, "the MCP key is accepted from this computer (loopback) only"
        if b"origin" in headers:
            return None, "the MCP key is not accepted from web pages"
        if self._matcher is None:
            self._matcher = catalog.Matcher(catalog.build(scope["app"].openapi()))
        tool = self._matcher.match(scope["method"], scope["path"])
        if tool is None:
            return None, f"{scope['method']} {scope['path']} is not available to AI agents"
        gate = self.mcp() if self.mcp is not None else None
        if tool.gate is not None and (gate is None or not gate.allows(tool.gate)):
            return None, catalog.GATES[tool.gate]
        return tool, ""
