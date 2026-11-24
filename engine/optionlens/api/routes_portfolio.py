"""/api/portfolio (docs/INTEGRATION.md §11): the user's positions from Questrade and IBKR, read-only. AI agents reach
the two GETs only while the user allowed it (Settings → AI agents → "Allow agents to see my portfolio"); the
settings are the app's only."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from .. import live
from ..services import portfolio
from .deps import respond
from .json import ApiJSON

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
Currency = Literal["USD", "CAD"]


def _live_snapshot() -> dict:
    """The live values of what is held (read on the event loop, where the live hub runs)."""
    symbols, contracts = portfolio.live_keys()
    return live.instance().snapshot(symbols, contracts)


@router.get("")
async def get_portfolio(currency: Currency = Query("USD", description="Reporting currency of converted totals"),
                        refresh: bool = Query(False, description="Fetch Questrade's accounts now (else when the last "
                                                                 "fetch is older than 55 s)")):
    """Positions (live-valued), P/L, greeks, aggregates per underlying / account / broker / total, account balances,
    the FX rates the brokers gave, beta-weighted delta vs SPY and each source's state."""
    await portfolio.refresh_questrade(force=refresh)
    return await respond(portfolio.view, currency, _live_snapshot())


@router.get("/risk")
async def get_risk(underlying: str = Query(..., min_length=1, max_length=12, description="Underlying ticker")):
    """Risk graph of everything held on one underlying: P/L at the nearest expiry and today across underlying prices,
    each leg at its cost basis (calculator engine)."""
    return await respond(portfolio.risk, underlying, _live_snapshot())


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questrade: bool | None = None
    ibkr: bool | None = None


@router.get("/settings")
async def get_settings():
    """{questrade, ibkr}: "Show my positions" per broker (default on)."""
    return ApiJSON(portfolio.settings_view())


@router.put("/settings")
async def put_settings(body: SettingsIn):
    """Switch a broker's positions on / off (off: hidden and not fetched)."""
    try:
        return ApiJSON(portfolio.set_settings(body.model_dump(exclude_none=True)))
    except OSError as e:
        raise HTTPException(500, detail=f"Could not save the portfolio settings: {e}") from e
