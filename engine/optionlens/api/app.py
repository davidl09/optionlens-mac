"""FastAPI application (API only): routers, error mapping, bearer auth, CORS, body-size limit and
the scheduler lifespan. Unknown paths answer 404 JSON."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import db
from ..config import Settings, get_settings
from ..jobs import scheduler
from ..jobs.ondemand import UnknownSymbol
from ..net import FetchError
from ..services import alerts
from ..services.errors import BadRequest, NoChain, NotFound
from . import (
    routes_backtest,
    routes_calc,
    routes_ingest,
    routes_market,
    routes_reports,
    routes_symbol,
    routes_system,
    routes_user,
)
from .auth import BearerAuth
from .json import ApiJSON

log = logging.getLogger("optionlens.api")
MAX_BODY_BYTES = 64 * 1024 * 1024   # a 50k-contract ingest chain is ~15 MB
CORS_METHODS = ("GET", "POST", "PUT", "DELETE", "OPTIONS")
CORS_HEADERS = ("Authorization", "Content-Type")


def scheduler_enabled() -> bool:
    return os.environ.get("OPTIONLENS_NO_SCHEDULER") != "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    alerts.register_hooks()  # alert evaluation, EOD report snapshots and digests
    if scheduler_enabled():
        await scheduler.start()
    try:
        yield
    finally:
        if scheduler_enabled():
            await scheduler.stop()
        db.close()


def _detail(status: int, detail: str, code: str | None = None) -> ApiJSON:
    """Error body {"detail": message}, plus a machine-readable "code" where the UI acts on the error."""
    return ApiJSON({"detail": detail, **({"code": code} if code else {})}, status_code=status)


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", ()) if x not in ("body", "query", "path"))
        parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
    return "; ".join(parts) or "invalid request"


def _install_errors(app: FastAPI) -> None:
    @app.exception_handler(UnknownSymbol)
    async def unknown_symbol(_: Request, exc: UnknownSymbol):
        return _detail(404, f"unknown symbol {exc}")

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound):
        return _detail(404, str(exc), exc.code if isinstance(exc, NoChain) else None)

    @app.exception_handler(BadRequest)
    async def bad_request(_: Request, exc: BadRequest):
        return _detail(422, str(exc))

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError):
        return _detail(422, _validation_message(exc))

    @app.exception_handler(FetchError)
    async def upstream(_: Request, exc: FetchError):
        log.warning("upstream fetch failed: %s", exc)
        return _detail(502, f"upstream data source unavailable: {exc}")

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return _detail(exc.status_code, str(exc.detail))


class BodyLimit:
    """ASGI middleware: 413 when a request declares a body larger than `limit` bytes."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES):
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            length = next((v for k, v in scope["headers"] if k == b"content-length"), b"0")
            if length.isdigit() and int(length) > self.limit:
                await _detail(413, f"request body over {self.limit} bytes")(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    """The API app (bearer auth when settings carry a token)."""
    s = settings or get_settings()
    app = FastAPI(title="OptionLens", lifespan=lifespan, default_response_class=ApiJSON,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = s
    _install_errors(app)
    for module in (routes_system, routes_symbol, routes_calc, routes_backtest,
                   routes_reports, routes_market, routes_user, routes_ingest):
        app.include_router(module.router)
    app.add_middleware(BodyLimit)
    if s.auth_enabled:
        app.add_middleware(BearerAuth, token=s.api_token)
    app.add_middleware(CORSMiddleware, allow_origins=list(s.cors_origins), allow_methods=list(CORS_METHODS),
                       allow_headers=list(CORS_HEADERS), allow_credentials=False)   # outermost: preflights skip auth
    return app
