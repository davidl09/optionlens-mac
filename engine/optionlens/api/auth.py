"""Bearer-token authentication (docs/INTEGRATION.md §2): when the app passed OPTIONLENS_API_TOKEN,
every request except `GET /api/health` must carry `Authorization: Bearer <token>`; comparison is
constant-time. CORS preflights are answered by the CORS middleware, which wraps this one."""
from __future__ import annotations

import hmac

from starlette.types import ASGIApp, Receive, Scope, Send

from .json import ApiJSON

OPEN_PATHS = frozenset({("GET", "/api/health"), ("HEAD", "/api/health")})


def token_ok(header: str | None, token: str) -> bool:
    """Whether an Authorization header value is `Bearer <token>` (constant-time on the token)."""
    scheme, _, given = (header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), token.encode())


class BearerAuth:
    """ASGI middleware: 401 {"detail": "unauthorized"} unless the request is open or authorized."""

    def __init__(self, app: ASGIApp, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (scope["method"], scope["path"]) not in OPEN_PATHS:
            header = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), None)
            if not token_ok(header, self.token):
                resp = ApiJSON({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)
