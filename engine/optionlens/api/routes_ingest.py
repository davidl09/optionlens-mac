"""/api/ingest/* (docs/INTEGRATION.md §6): data pushed by the IBKR bridge, and GET /api/ingest/wants.
Bodies are validated here (types, ranges, size limits); services.ingest stores them."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from ..models import normalize_symbol
from ..services import ingest
from .deps import respond, symbol_param

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

Symbol = Annotated[str, AfterValidator(lambda s: symbol_param(normalize_symbol(s)))]
Source = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")]
Price = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Num = Annotated[float, Field(allow_inf_nan=False)]
Count = Annotated[float, Field(ge=0, allow_inf_nan=False)]   # volumes/OI (IBKR sends some as floats)
Strike = Annotated[float, Field(gt=0, lt=100_000, allow_inf_nan=False)]


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class UnderlyingIn(_In):
    price: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    bid: Price | None = None
    ask: Price | None = None
    open: Price | None = None
    high: Price | None = None
    low: Price | None = None
    prev_close: Price | None = None
    volume: Count | None = None
    ts: AwareDatetime | None = None
    rth_close: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None   # regular-session close (final chains)


class ContractIn(_In):
    expiry: date
    strike: Strike
    right: Literal["C", "P"]
    bid: Price | None = None
    ask: Price | None = None
    last: Price | None = None
    last_ts: AwareDatetime | None = None   # when `last` traded
    volume: Count | None = None
    oi: Count | None = None
    iv: Num | None = None
    delta: Num | None = None
    gamma: Num | None = None
    theta: Num | None = None
    vega: Num | None = None


class ChainIn(_In):
    source: Source
    delayed: bool                       # pushed from delayed market data (INTEGRATION.md §6)
    symbol: Symbol
    fetched_at: AwareDatetime
    final: bool = False
    session_date: date | None = None
    underlying: UnderlyingIn
    contracts: list[ContractIn] = Field(max_length=ingest.MAX_CONTRACTS)


class NoOptionsIn(_In):
    source: Source
    symbol: Symbol


class PointIn(_In):
    date: date
    value: float


class VolHistoryIn(_In):
    source: Source
    symbol: Symbol
    kind: str
    bars: list[PointIn] = Field(max_length=ingest.MAX_BARS)


class IvHistoryIn(VolHistoryIn):
    kind: Literal["iv30"] = "iv30"


class HvHistoryIn(VolHistoryIn):
    kind: Literal["hv30"] = "hv30"


class IndexBarIn(_In):
    date: date
    open: Price | None = None
    high: Price | None = None
    low: Price | None = None
    close: Price


class IndexBarsIn(_In):
    source: Source
    symbol: Symbol
    bars: list[IndexBarIn] = Field(max_length=ingest.MAX_BARS)


class QuoteIn(_In):
    symbol: Symbol
    price: Price | None = None
    change: Num | None = None
    change_pct: Num | None = None
    bid: Price | None = None
    ask: Price | None = None
    open: Price | None = None
    high: Price | None = None
    low: Price | None = None
    prev_close: Price | None = None
    volume: Count | None = None
    ts: AwareDatetime


class QuotesIn(_In):
    source: Source
    delayed: bool
    quotes: list[QuoteIn] = Field(max_length=ingest.MAX_QUOTES)


class HeartbeatIn(_In):
    client: Annotated[str, Field(min_length=1, max_length=200)]
    ibkr_connected: bool
    account_type: Literal["live", "paper"] | None = None


class StatIn(_In):
    symbol: Symbol
    price: Price | None = None
    change_pct: Num | None = None
    volume: Count | None = None
    call_volume: Count | None = None
    put_volume: Count | None = None
    call_oi: Count | None = None
    put_oi: Count | None = None
    iv30: Num | None = None
    hv30: Num | None = None


class UnderlyingStatsIn(_In):
    source: Source
    delayed: bool
    session_date: date
    ts: AwareDatetime
    stats: list[StatIn] = Field(max_length=ingest.MAX_QUOTES)


class OptionBarIn(_In):
    expiry: date
    strike: Strike
    right: Literal["C", "P"]
    date: date
    open: Price | None = None
    high: Price | None = None
    low: Price | None = None
    close: Price
    volume: Count | None = None


class OptionBarsIn(_In):
    source: Source
    symbol: Symbol
    bars: list[OptionBarIn] = Field(max_length=ingest.MAX_CONTRACTS)


class AckItemIn(_In):
    symbol: Symbol
    sessions: list[date] = Field(min_length=1, max_length=60)


class AckWantsIn(_In):
    kind: Literal["option_bars"]
    items: list[AckItemIn] = Field(max_length=ingest.MAX_QUOTES)


@router.post("/chain")
async def chain(req: ChainIn):
    return await respond(ingest.chain, req.model_dump())


@router.post("/no-options")
async def no_options(req: NoOptionsIn):
    return await respond(ingest.no_options, req.model_dump())


@router.post("/iv-history")
async def iv_history(req: IvHistoryIn):
    return await respond(ingest.vol_history, "iv30", req.model_dump())


@router.post("/hv-history")
async def hv_history(req: HvHistoryIn):
    return await respond(ingest.vol_history, "hv30", req.model_dump())


@router.post("/index-bars")
async def index_bars(req: IndexBarsIn):
    return await respond(ingest.index_bars, req.model_dump())


@router.post("/quotes")
async def quotes(req: QuotesIn):
    return await respond(ingest.quotes, req.model_dump())


@router.post("/heartbeat")
async def heartbeat(req: HeartbeatIn):
    return await respond(ingest.heartbeat, req.model_dump())


@router.post("/underlying-stats")
async def underlying_stats(req: UnderlyingStatsIn):
    return await respond(ingest.underlying_stats, req.model_dump())


@router.post("/option-bars")
async def option_bars(req: OptionBarsIn):
    return await respond(ingest.option_bars, req.model_dump())


@router.post("/ack-wants")
async def ack_wants(req: AckWantsIn):
    return await respond(ingest.ack_wants, req.model_dump())


@router.get("/wants")
async def wants():
    return await respond(ingest.wants)

