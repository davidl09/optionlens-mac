"""/api/health, /api/status, /api/search, /api/compare/volatility."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..services import compare, system
from .deps import respond, symbols_list

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health(request: Request):
    """Open (no auth): liveness, auth mode and server id (INTEGRATION.md §2)."""
    return await respond(system.health, request.app.state.settings)


@router.get("/status")
async def status():
    return await respond(system.status)


@router.get("/search")
async def search(q: str = ""):
    return await respond(system.search, q)


@router.get("/compare/volatility")
async def compare_volatility(symbols: str):
    return await respond(compare.volatility, symbols_list(symbols))
