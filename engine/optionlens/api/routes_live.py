"""/api/live (docs/INTEGRATION.md §9): a renderer window subscribes its visible set and reads its push channel.

Auth: the stream is an ordinary GET under the same Bearer token (the renderer reads it with fetch streaming, which can
send the header; EventSource cannot, so no token ever goes in a URL)."""
from __future__ import annotations

from datetime import date

import anyio.to_thread
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from .. import live
from ..live.resolve import MAX_CONTRACTS, MAX_SYMBOLS, resolve
from .deps import symbol_param
from .json import ApiJSON

router = APIRouter(prefix="/api/live", tags=["live"])
CLIENT_ID = r"^[A-Za-z0-9_-]{8,64}$"


class ChainViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    expiry: date
    strike_min: float | None = None
    strike_max: float | None = None


class SubscribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(pattern=CLIENT_ID)
    symbols: list[str] = Field(default_factory=list, max_length=MAX_SYMBOLS)
    option_contracts: list[str] = Field(default_factory=list, max_length=MAX_CONTRACTS)
    chains: list[ChainViewIn] = Field(default_factory=list, max_length=8)


@router.post("/subscribe")
async def subscribe(body: SubscribeIn):
    """Replace the window's visible set (an empty body unsubscribes)."""
    symbols = [symbol_param(s) for s in body.symbols]
    views = [(symbol_param(v.symbol), v.expiry, v.strike_min, v.strike_max) for v in body.chains]
    wanted = await anyio.to_thread.run_sync(resolve, symbols, body.option_contracts, views)
    return ApiJSON(live.instance().subscribe(body.client_id, wanted))


@router.get("/stream")
async def stream(client_id: str = Query(pattern=CLIENT_ID)):
    """text/event-stream: `status` and `patch` events for the window (docs §9)."""
    return StreamingResponse(live.instance().events(client_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/status")
async def status():
    return ApiJSON(live.instance().status())
