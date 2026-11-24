"""/api/calc/strategy and /api/strategy-templates (SPEC §8 calculator)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services import calc
from .deps import respond

router = APIRouter(prefix="/api", tags=["calc"])


class LegIn(BaseModel):
    kind: Literal["stock", "option"]
    right: Literal["C", "P"] | None = None
    strike: float | None = None
    expiry: date | None = None
    qty: float
    price: float | None = None
    iv: float | None = None


class CalcRequest(BaseModel):
    symbol: str
    legs: list[LegIn] = Field(min_length=1)
    iv_shift: float | None = None
    eval_date: date | None = None
    price_range: tuple[float, float] | None = None
    points: int | None = None
    spot: float | None = Field(default=None, gt=0)   # live underlying price (else the stored chain's)


@router.post("/calc/strategy")
async def calc_strategy(req: CalcRequest):
    return await respond(calc.calculate, req.model_dump())


@router.get("/strategy-templates")
async def strategy_templates():
    return await respond(calc.templates)
