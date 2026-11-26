"""/api/ingest/* (docs/INTEGRATION.md §6): data pushed by the IBKR bridge, and GET /api/ingest/wants.
Bodies are validated here (types, ranges, size limits); services.ingest stores them."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from .. import live
from ..models import normalize_symbol
from ..services import ingest, intraday, portfolio, refresh
from .deps import respond, symbol_param
from .json import ApiJSON

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
    # `price` is the engine's reference price (GET /api/ingest/reference-price, `ts` its time): the client's feed sent
    # none. It centres the chain and its computed IVs; it becomes the stored quote only when there is none.
    reference: bool = False


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
    # The contract list is a saved one (IBKR's contract server was unavailable), listed at contracts_listed_at.
    contracts_from_cache: bool = False
    contracts_listed_at: AwareDatetime | None = None
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


FarmState = Literal["ok", "inactive", "connecting", "down", "unknown"]


class FarmsIn(_In):
    market_data: FarmState = "unknown"
    historical: FarmState = "unknown"
    sec_def: FarmState = "unknown"


class HeartbeatIn(_In):
    client: Annotated[str, Field(min_length=1, max_length=200)]
    ibkr_connected: bool
    account_type: Literal["live", "paper"] | None = None
    farms: FarmsIn | None = None                      # IBKR server farms as the Gateway reports them
    options_subscription: Literal["unknown", "realtime", "delayed_only"] | None = None


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


class CloseIn(_In):
    date: date
    close: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class DividendHistoryIn(_In):
    """IBKR daily closes of one symbol (INTEGRATION.md §6 "Dividends"): `adjusted` = whatToShow ADJUSTED_LAST,
    `trades` = TRADES, both 1 day bars with useRTH."""
    source: Source
    symbol: Symbol
    adjusted: list[CloseIn] = Field(max_length=ingest.MAX_BARS)
    trades: list[CloseIn] = Field(max_length=ingest.MAX_BARS)


@router.post("/dividend-history")
async def dividend_history(req: DividendHistoryIn):
    """Dividends derived from IBKR's adjusted vs raw daily closes (stored with source 'ibkr-adjusted')."""
    return await respond(ingest.dividend_history, req.model_dump())


class IntradayBarIn(_In):
    ts: AwareDatetime                    # the bar's start
    open: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    high: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    low: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    close: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    volume: Count | None = None


class SpanIn(_In):
    start: AwareDatetime
    end: AwareDatetime


class IntradayBarsIn(_In):
    source: Source
    symbol: Symbol
    interval: Literal["1m", "5m", "15m", "1h"]
    delayed: bool | None = None          # null: unknown
    spans: list[SpanIn] = Field(min_length=1, max_length=intraday.MAX_PUSH_SPANS)   # what was fetched (all hours)
    bars: list[IntradayBarIn] = Field(max_length=intraday.MAX_PUSH_BARS)


@router.post("/intraday-bars")
async def intraday_bars(req: IntradayBarsIn):
    """Intraday bars a client fetched for the `missing` spans of GET /bars?interval=… (docs §5 "Intraday bars")."""
    return await respond(intraday.store_pushed, req.model_dump())


class RefreshResultIn(_In):
    source: Source
    symbol: Symbol
    status: Literal["pending", "refreshed", "fresh", "no-options", "unavailable", "failed"]
    message: Annotated[str, Field(max_length=500)] | None = None
    waiting_for: Literal["contract-server", "market-hours"] | None = None


@router.post("/refresh-result")
async def refresh_result(req: RefreshResultIn):
    """The bridge's outcome of an on-demand refresh (docs/INTEGRATION.md §7)."""
    return await respond(refresh.report, req.symbol, req.source, req.status, req.message, req.waiting_for)


@router.get("/reference-price/{sym}")
async def reference_price(sym: str = Depends(symbol_param)):
    return await respond(ingest.reference_price, sym)


@router.get("/wants")
async def wants():
    return await respond(ingest.wants)



@router.get("/live-wants")
async def live_wants():
    """The visible symbols the bridge should snapshot now (only while it is the live fallback, docs §9), and whether
    the engine wants the IBKR positions feed (`positions`: "Show my positions" for IBKR, docs §11)."""
    return ApiJSON({**live.instance().bridge_wants(), "positions": portfolio.wanted("ibkr")})


Account = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")]
Money = Annotated[float, Field(allow_inf_nan=False)]
Ccy = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")]


class PositionIn(_In):
    account: Account
    sec_type: Annotated[str, Field(min_length=1, max_length=8)]     # STK, OPT, FUT, CASH, ... (as TWS names it)
    symbol: Annotated[str, Field(min_length=1, max_length=24)]
    currency: Ccy = "USD"
    qty: Num
    avg_cost: Num | None = None          # as TWS reports it: per share (STK), per contract (OPT, x multiplier)
    multiplier: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    expiry: date | None = None
    strike: Strike | None = None
    right: Literal["C", "P"] | None = None
    trading_class: Annotated[str, Field(max_length=12)] | None = None
    con_id: int | None = None


class AccountSummaryIn(_In):
    account: Account
    currency: Ccy = "USD"
    net_liquidation: Money | None = None
    cash: Money | None = None
    buying_power: Money | None = None


class FxIn(_In):
    currency: Ccy
    base: Ccy
    rate: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class PositionsIn(_In):
    source: Literal["ibkr"]
    fetched_at: AwareDatetime | None = None
    positions: list[PositionIn] = Field(default_factory=list, max_length=5000)
    accounts: list[AccountSummaryIn] = Field(default_factory=list, max_length=100)
    fx: list[FxIn] = Field(default_factory=list, max_length=100)


@router.post("/positions")
async def ingest_positions(body: PositionsIn):
    """The IBKR accounts' positions (reqPositions), account summary and exchange rates (reqAccountSummary), read-only
    (docs §6, §11). Replaces IBKR's previous push; ignored while "Show my positions" is off for IBKR. Account numbers
    are masked in every answer and never logged."""
    return await respond(portfolio.ingest_ibkr, body.model_dump())
