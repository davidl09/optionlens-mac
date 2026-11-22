"""/api/sources/questrade (docs/INTEGRATION.md §8): connect Questrade with a pasted refresh token, forget it, and
its status. The token is accepted in the body only and never echoed back."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ..services import sources
from .deps import respond
from .json import ApiJSON

router = APIRouter(prefix="/api/sources", tags=["sources"])


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    refresh_token: str = Field(min_length=1, max_length=512)


@router.get("/questrade")
async def questrade_status():
    return await respond(sources.questrade_status)


@router.post("/questrade/token")
async def questrade_connect(body: TokenIn):
    return ApiJSON(await sources.connect_questrade(body.refresh_token))


@router.delete("/questrade/token")
async def questrade_disconnect():
    return await respond(sources.disconnect_questrade)
