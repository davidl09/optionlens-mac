"""/api/reports (definitions), /api/reports/{id} (ReportResult) and /api/compare/straddle."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..services.reports import registry
from .deps import respond, symbols_list
from .json import ApiJSON

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/reports")
async def report_definitions():
    return ApiJSON(registry.definitions())


@router.get("/reports/{report_id}")
async def report(report_id: str, request: Request):
    return ApiJSON(await registry.run_report(report_id, dict(request.query_params)))


@router.get("/compare/straddle")
async def compare_straddle(symbols: str, dte: int = 30):
    return await respond(registry.compare_straddle, symbols_list(symbols), dte)
