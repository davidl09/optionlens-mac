"""FastAPI application: API routers, error mapping, scheduler lifespan and the built SPA (web/dist)."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, PlainTextResponse, Response

from .. import db
from ..config import PACKAGE_DIR
from ..jobs import scheduler
from ..jobs.ondemand import UnknownSymbol
from ..net import FetchError
from ..services import alerts
from ..services.errors import BadRequest, NotFound
from . import routes_backtest, routes_calc, routes_market, routes_reports, routes_symbol, routes_system, routes_user
from .json import ApiJSON

log = logging.getLogger("optionlens.api")
WEB_DIST = PACKAGE_DIR.parent.parent / "web" / "dist"
NO_SPA = "OptionLens API is running, but the web app is not built.\nBuild it with: cd web && npm ci && npm run build\n"


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


def _detail(status: int, detail: str) -> ApiJSON:
    return ApiJSON({"detail": detail}, status_code=status)


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
        return _detail(404, str(exc))

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


def _install_spa(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            return _detail(404, "not found")
        index = root / "index.html"
        if not index.is_file():
            return PlainTextResponse(NO_SPA)
        f = (root / path).resolve()
        if path and f.is_file() and f.is_relative_to(root):
            return FileResponse(f)
        return FileResponse(index)


def create_app(web_dist: Path = WEB_DIST) -> FastAPI:
    app = FastAPI(title="OptionLens", lifespan=lifespan, default_response_class=ApiJSON)
    _install_errors(app)
    for module in (routes_system, routes_symbol, routes_calc, routes_backtest,
                   routes_reports, routes_market, routes_user):
        app.include_router(module.router)
    _install_spa(app, web_dist)
    return app
