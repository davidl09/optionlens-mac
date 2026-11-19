"""/api/backtest/stock and /api/backtest/options (SPEC §9)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services import backtests
from .deps import respond, symbol_param

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class StockEntry(BaseModel):
    type: str
    n: int | None = None
    value: float | None = None
    direction: Literal["up", "down", "above", "below", "high", "low"] | None = None
    month: int | None = Field(None, ge=1, le=12)
    day: int | None = Field(None, ge=1, le=31)


class StockExit(BaseModel):
    hold_days: int | None = Field(None, ge=1)
    at_earnings_reaction: bool | None = None
    stop_loss: float | None = Field(None, gt=0)
    take_profit: float | None = Field(None, gt=0)


class StockBacktestRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    entry: StockEntry
    exit: StockExit
    side: Literal["long", "short"]
    start: date | None = None
    end: date | None = None


class StrikeRule(BaseModel):
    type: Literal["delta", "pct_otm", "atm"]
    value: float | None = None


class OptionsEntry(BaseModel):
    type: Literal["every_n_days", "monthly", "weekly"]
    n: int | None = Field(None, ge=1)
    weekday: int | None = Field(None, ge=0, le=4)


class OptionsExit(BaseModel):
    type: Literal["expiry", "dte"]
    dte: int | None = Field(None, ge=0)
    take_profit: float | None = Field(None, gt=0)
    stop_loss: float | None = Field(None, gt=0)


class OptionsBacktestRequest(BaseModel):
    symbol: str
    strategy: str
    strike_rule: StrikeRule
    width_pct: float | None = None
    entry: OptionsEntry
    target_dte: int
    exit: OptionsExit
    start: date | None = None
    end: date | None = None
    qty: float | None = Field(None, gt=0)


@router.post("/stock")
async def stock(req: StockBacktestRequest):
    body = req.model_dump()
    body["symbols"] = [symbol_param(s) for s in req.symbols]
    return await respond(backtests.stock, body)


@router.post("/options")
async def options(req: OptionsBacktestRequest):
    body = req.model_dump()
    body["symbol"] = symbol_param(req.symbol)
    return await respond(backtests.options, body)
