"""Per-symbol earnings payloads (types.ts): Earnings, NextEarnings, EarningsStrategyResult and the
per-strategy BacktestTrade list."""
from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from ..analytics import calendar as cal
from ..analytics.calendar import now_ny
from ..analytics import chainmetrics as cm
from ..analytics import earnings as earn_a
from ..analytics import optbacktest
from ..analytics.chain import ChainArrays
from ..analytics.stats import bar_arrays
from ..models import Bar, EarningsEvent
from ..repo import earnings as earn_repo
from ..repo import options as options_repo
from . import context as ctx
from .errors import BadRequest, NotFound

STATS_LAST = 12
MAX_LOOKBACK = 12
ENTRY_RANGE = (-10, -1)    # trading days before the reaction day
EXIT_RANGE = (0, 10)       # trading days after the reaction day


def _upcoming(symbol: str) -> EarningsEvent | None:
    ev = ctx.next_earnings_map().get(symbol)
    return ev if ev is not None and is_pending(ev) else None


def is_pending(ev: EarningsEvent, now: datetime | None = None) -> bool:
    """The event's (expected) reaction session has not started yet: reaction day after today, or
    today before the open. A bmo event once today's session runs is past (no post-event straddle)."""
    now = now or now_ny()
    r = reaction_day(ev)
    return r > now.date() or (r == now.date() and now.time() < cal.OPEN)


def _past(events: Sequence[EarningsEvent]) -> list[EarningsEvent]:
    """Events whose reaction session has started (stored history incl. today's bmo after the open)."""
    t = ctx.today()
    return [e for e in events if e.date <= t and not is_pending(e)]


def reaction_day(ev: EarningsEvent) -> date:
    """Expected reaction session of a future event (context.expected_reaction_day)."""
    return ctx.expected_reaction_day(ev)


def next_reaction_date(symbol: str) -> date | None:
    ev = _upcoming(symbol)
    return reaction_day(ev) if ev else None


def _past_rows(symbol: str, events: Sequence[EarningsEvent], bars: Sequence[Bar]) -> list[dict]:
    iv30 = ctx.metrics_series(symbol, ["iv30"])["iv30"]
    return earn_a.event_rows(_past(events), bars,
                             implied_moves=_stored_implied(symbol, events, bars), iv30_by_date=iv30)


def _stored_implied(symbol: str, events: Sequence[EarningsEvent], bars: Sequence[Bar]) -> dict[date, float]:
    """Implied earnings move from the stored EOD chain of the pre-event close (when archived)."""
    eod_days = set(options_repo.eod_dates(symbol))
    if not eod_days:
        return {}
    a = bar_arrays(bars)
    out = {}
    for ev in events:
        react, _ = earn_a.reaction_from_arrays(a, ev.date, ev.timing)
        if react is None or (i := a.index_of(react)) is None or i == 0 or a.dates[i - 1] not in eod_days:
            continue
        ca = ctx.eod_chain_arrays(symbol, a.dates[i - 1])
        if ca is not None:
            v = earn_a.implied_earnings_move(cm.expiry_term_rows(ca), react, ca.spot)["implied_move_pct"]
            if v is not None:
                out[ev.date] = v
    return out


def next_earnings(symbol: str, ca: ChainArrays | None, bars: Sequence[Bar],
                  rows: list[dict] | None = None) -> dict | None:
    """Earnings.next (NextEarnings + excess implied, implied/historical ratio, expiry used)."""
    ev = _upcoming(symbol)
    if ev is None:
        return None
    if rows is None:
        rows = _past_rows(symbol, earn_repo.for_symbol(symbol), bars)
    avg_abs = earn_a.earnings_stats(rows[:STATS_LAST])["avg_abs_move"]
    imp = {"implied_move_pct": None, "excess_implied_move_pct": None, "expiry_used": None}
    if ca is not None:
        imp = earn_a.implied_earnings_move(ctx.derived(ca, "term_rows", cm.expiry_term_rows), reaction_day(ev), ca.spot)
    return {"date": ev.date, "timing": ev.timing, "days": (ev.date - ctx.today()).days,
            "implied_move_pct": imp["implied_move_pct"], "avg_abs_move_pct": avg_abs,
            "excess_implied_move_pct": imp["excess_implied_move_pct"],
            "implied_vs_hist_ratio": earn_a.implied_vs_hist_ratio(imp["implied_move_pct"], avg_abs),
            "expiry_used": imp["expiry_used"]}


def earnings(symbol: str) -> dict:
    sym = ctx.refresh(symbol)["symbol"]
    bars = ctx.load_bars(sym)
    rows = _past_rows(sym, earn_repo.for_symbol(sym), bars)
    ca = ctx.load_chain_arrays(sym, ensure_fresh=False)
    return {"symbol": sym, "next": next_earnings(sym, ca, bars, rows), "events": rows,
            "stats": earn_a.earnings_stats(rows[:STATS_LAST]), "stats_all": earn_a.earnings_stats(rows)}


# ----------------------------------------------------------------------------- strategy backtests
def _check(lookback: int, entry: int, exit: int) -> None:
    if not 1 <= lookback <= MAX_LOOKBACK:
        raise BadRequest(f"lookback must be 1..{MAX_LOOKBACK}")
    if not ENTRY_RANGE[0] <= entry <= ENTRY_RANGE[1]:
        raise BadRequest(f"entry must be {ENTRY_RANGE[0]}..{ENTRY_RANGE[1]} trading days")
    if not EXIT_RANGE[0] <= exit <= EXIT_RANGE[1]:
        raise BadRequest(f"exit must be {EXIT_RANGE[0]}..{EXIT_RANGE[1]} trading days")


def _inputs(symbol: str) -> dict:
    row = ctx.ensure_known(symbol)
    sym = row["symbol"]
    bars = ctx.load_bars(sym, live=False)
    spot = bars[-1].close if bars else None
    return {"symbol": sym, "bars": bars, "events": _past(earn_repo.for_symbol(sym)),
            "provider": ctx.DbPriceProvider(), "rate": ctx.rate_fn(),
            "div_yield": ctx.dividend_yield_map({sym: spot}).get(sym, 0.0) if spot else 0.0,   # bulk inputs, cached
            "is_index": row["kind"] == "index", "universe_event_var": ctx.universe_event_variance()}


def strategies(symbol: str, lookback: int = 12, entry: int = -1, exit: int = 1) -> dict:
    _check(lookback, entry, exit)
    x = _inputs(symbol)
    sym = x.pop("symbol")
    return optbacktest.earnings_strategy_backtest(sym, x.pop("bars"), x.pop("events"), lookback=lookback,
                                                  entry=entry, exit=exit, **x)


def strategy_trades(symbol: str, strategy: str, lookback: int = 12, entry: int = -1, exit: int = 1) -> list[dict]:
    _check(lookback, entry, exit)
    if strategy not in optbacktest.EARNINGS_STRATEGY_IDS:
        raise NotFound(f"unknown earnings strategy {strategy!r}")
    x = _inputs(symbol)
    sym = x.pop("symbol")
    return optbacktest.earnings_strategy_trades(sym, x.pop("bars"), x.pop("events"), strategy, lookback=lookback,
                                                entry=entry, exit=exit, **x)
