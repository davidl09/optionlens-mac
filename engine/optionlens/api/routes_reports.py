"""/api/reports (definitions), /api/reports/{id} (ReportResult) and /api/compare/straddle."""
from __future__ import annotations

from fastapi import APIRouter, Path, Request

from ..services.reports import registry
from .deps import respond, symbols_list
from .json import ApiJSON

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/reports")
async def report_definitions():
    return ApiJSON(registry.definitions())


# The report ids, in the schema (OpenAPI, the MCP tools): validated by the registry (404 for an unknown id).
ReportId = Path(..., description="Report id (GET /api/reports)", json_schema_extra={"enum": list(registry.REPORTS)})


@router.get("/reports/{report_id}")
async def report(request: Request, report_id: str = ReportId):
    """ReportResult; the report's params (ReportDef.params) as query parameters, defaults for missing ones."""
    return ApiJSON(await registry.run_report(report_id, dict(request.query_params)))


@router.get("/compare/straddle")
async def compare_straddle(symbols: str, dte: int = 30):
    return await respond(registry.compare_straddle, symbols_list(symbols), dte)
