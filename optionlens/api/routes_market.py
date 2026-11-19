"""/api/market/overview."""
from __future__ import annotations

from fastapi import APIRouter

from ..services import market
from .deps import respond

router = APIRouter(prefix="/api", tags=["market"])


@router.get("/market/overview")
async def overview():
    return await respond(market.overview)
