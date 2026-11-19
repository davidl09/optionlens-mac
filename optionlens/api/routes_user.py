"""/api/watchlists, /api/presets, /api/alerts, /api/notifications (bodies per types.ts)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel

from ..services import user
from .deps import respond, symbol_param

router = APIRouter(prefix="/api", tags=["user"])


class WatchlistIn(BaseModel):
    name: str | None = None
    symbols: list[str] | None = None


class PresetIn(BaseModel):
    report_id: str
    name: str
    payload: dict[str, Any]


class AlertIn(BaseModel):
    kind: str | None = None
    symbol: str | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    cooldown_min: int | None = None


class ReadIn(BaseModel):
    ids: list[int] | None = None


# ----------------------------------------------------------------------------- watchlists
@router.get("/watchlists")
async def watchlists():
    return await respond(user.list_watchlists)


@router.post("/watchlists")
async def create_watchlist(body: WatchlistIn):
    return await respond(user.create_watchlist, body.name, body.symbols)


@router.get("/watchlists/{wid}")
async def get_watchlist(wid: int):
    return await respond(user.get_watchlist, wid)


@router.put("/watchlists/{wid}")
async def update_watchlist(wid: int, body: WatchlistIn):
    return await respond(user.update_watchlist, wid, body.name, body.symbols)


@router.delete("/watchlists/{wid}")
async def delete_watchlist(wid: int):
    return await respond(user.delete_watchlist, wid)


@router.post("/watchlists/{wid}/symbols/{sym}")
async def add_symbol(wid: int, sym: str):
    return await respond(user.add_symbol, wid, symbol_param(sym))


@router.delete("/watchlists/{wid}/symbols/{sym}")
async def remove_symbol(wid: int, sym: str):
    return await respond(user.remove_symbol, wid, symbol_param(sym))


@router.get("/watchlists/{wid}/table")
async def watchlist_table(wid: int):
    return await respond(user.watchlist_table, wid)


# ----------------------------------------------------------------------------- presets
@router.get("/presets")
async def presets(report_id: str | None = None):
    return await respond(user.list_presets, report_id)


@router.post("/presets")
async def create_preset(body: PresetIn):
    return await respond(user.create_preset, body.report_id, body.name, body.payload)


@router.delete("/presets/{pid}")
async def delete_preset(pid: int):
    return await respond(user.delete_preset, pid)


# ----------------------------------------------------------------------------- alerts
@router.get("/alerts")
async def alerts():
    return await respond(user.list_alerts)


@router.post("/alerts")
async def create_alert(body: AlertIn):
    return await respond(user.create_alert, body.kind, body.symbol, body.params or {},
                         True if body.enabled is None else body.enabled,
                         240 if body.cooldown_min is None else body.cooldown_min)


@router.put("/alerts/{aid}")
async def update_alert(aid: int, body: AlertIn):
    return await respond(user.update_alert, aid, body.model_dump(exclude_unset=True))


@router.delete("/alerts/{aid}")
async def delete_alert(aid: int):
    return await respond(user.delete_alert, aid)


# ----------------------------------------------------------------------------- notifications
@router.get("/notifications")
async def notifications(unread: int = 0):
    return await respond(user.list_notifications, bool(unread))


@router.post("/notifications/read")
async def mark_read(body: ReadIn | None = Body(default=None)):
    return await respond(user.mark_read, body.ids if body else None)
