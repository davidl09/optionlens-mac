"""/api/symbol/{sym}/... — per-symbol pages (SPEC §8, §10)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from ..services import refresh, symbol, symbol_company, symbol_earnings, symbol_options, symbol_vol
from .deps import respond, symbol_param
from .json import ApiJSON

router = APIRouter(prefix="/api/symbol/{sym}", tags=["symbol"])
Sym = Depends(symbol_param)
Range = Literal["1m", "3m", "6m", "1y", "3y", "5y", "10y", "max"]


@router.post("/refresh")
async def refresh_start(sym: str = Sym):
    """Start an on-demand refresh round from every connected chain source (docs/INTEGRATION.md §7)."""
    return ApiJSON(await refresh.start(sym))


@router.get("/refresh")
async def refresh_status(sym: str = Sym):
    return await respond(refresh.status, sym)


@router.get("/summary")
async def summary(sym: str = Sym):
    return await respond(symbol.summary, sym)


@router.get("/bars")
async def bars(sym: str = Sym, range: Range = "1y"):
    return await respond(symbol.bars, sym, range)


@router.get("/price-action")
async def price_action(sym: str = Sym, range: Range = "1y"):
    return await respond(symbol.price_action, sym, range)


@router.get("/chain")
async def chain(sym: str = Sym, expiry: str | None = None):
    return await respond(symbol_options.chain, sym, expiry)


@router.get("/activity")
async def activity(sym: str = Sym, expiry: str | None = None):
    return await respond(symbol_options.activity, sym, expiry)


@router.get("/open-interest")
async def open_interest(sym: str = Sym, expiry: str | None = None):
    return await respond(symbol_options.open_interest, sym, expiry)


@router.get("/contract/{osi}")
async def contract(osi: str, sym: str = Sym):
    return await respond(symbol_options.contract, sym, osi)


@router.get("/volatility")
async def volatility(sym: str = Sym):
    return await respond(symbol_vol.volatility, sym)


@router.get("/skew")
async def skew(sym: str = Sym, expiry: str | None = None):
    return await respond(symbol_vol.skew, sym, expiry)


@router.get("/implied-moves")
async def implied_moves(sym: str = Sym):
    return await respond(symbol_options.implied_moves, sym)


@router.get("/vol-seasonality")
async def vol_seasonality(sym: str = Sym):
    return await respond(symbol_vol.vol_seasonality, sym)


@router.get("/earnings")
async def earnings(sym: str = Sym):
    return await respond(symbol_earnings.earnings, sym)


@router.get("/earnings/strategies")
async def earnings_strategies(sym: str = Sym, lookback: int = 12, entry: int = -1, exit: int = 1):
    return await respond(symbol_earnings.strategies, sym, lookback, entry, exit)


@router.get("/earnings/strategies/{strategy}/trades")
async def earnings_strategy_trades(strategy: str, sym: str = Sym, lookback: int = 12, entry: int = -1, exit: int = 1):
    return await respond(symbol_earnings.strategy_trades, sym, strategy, lookback, entry, exit)


@router.get("/dividends")
async def dividends(sym: str = Sym):
    return await respond(symbol.dividends, sym)


@router.get("/seasonality")
async def seasonality(sym: str = Sym):
    return await respond(symbol.seasonality, sym)


@router.get("/events")
async def events(sym: str = Sym):
    return await respond(symbol.events, sym)


@router.get("/straddle")
async def straddle(sym: str = Sym):
    return await respond(symbol_options.straddle, sym)


@router.get("/ideas")
async def ideas(sym: str = Sym):
    return await respond(symbol_options.ideas, sym)


@router.get("/fundamentals")
async def fundamentals(sym: str = Sym, period: Literal["annual", "quarterly"] = "annual"):
    return await respond(symbol_company.fundamentals, sym, period)


@router.get("/peers")
async def peers(sym: str = Sym):
    return await respond(symbol_company.peers, sym)


@router.get("/insiders")
async def insiders(sym: str = Sym):
    return await respond(symbol_company.insiders, sym)


@router.get("/news")
async def news(sym: str = Sym):
    return await respond(symbol_company.news, sym)


@router.get("/profile")
async def profile(sym: str = Sym):
    return await respond(symbol_company.profile, sym)
