"""Ingest API (docs/INTEGRATION.md §6): data pushed by a client (the IBKR bridge) stored through the
same pipeline as the server's own collectors, and `wants` — what the server would like it to fetch.

Inputs are validated plain values (the API layer parses JSON); every write is an idempotent upsert.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..analytics import computed_iv
from ..analytics.calendar import NY, OPEN, last_completed_session, now_ny
from ..analytics.chain import ChainArrays
from ..jobs import bridge_plan, catchup, chains, hooks
from ..jobs._util import session_close, session_date
from ..jobs.marketdata import VIX_FAMILY
from ..models import Bar, ChainSnapshot, OptionContract, Quote, normalize_symbol
from ..repo import bars as bars_repo
from ..repo import bridge as bridge_repo
from ..repo import market as market_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..repo import user as user_repo
from ..repo import vol_history as vol_history_repo
from ..sources.osi import format_osi
from . import context as ctx
from .errors import BadRequest

MAX_CONTRACTS = 50_000          # per chain post
MAX_BARS = 20_000               # per history post (~80 years of daily bars)
MAX_QUOTES = 5_000              # per quotes post
CLOCK_SKEW = timedelta(minutes=5)
IV_HISTORY_YEARS = 10
SWEEP_HOOK_INTERVAL_S = 600     # pushed chains/stats stand in for sweeps: on_sweep_complete at most every 10 min
sweep_hooks = hooks.Debounced(hooks.on_sweep_complete, SWEEP_HOOK_INTERVAL_S)


def _sweep_done(day: date, symbols: list[str]) -> None:
    """Schedule the (debounced) on_sweep_complete hooks — alerts, frame refresh — on the server loop."""
    ctx.run_async(sweep_hooks.notify, day, symbols)


# ----------------------------------------------------------------------------- chains
def _quote(symbol: str, u: dict, fetched_at: datetime, delayed: bool, final: bool) -> Quote:
    """The underlying quote of a pushed chain. A closing snapshot uses the regular-session close
    (`rth_close`) when given: after 16:00 the live price is an extended-hours trade, which must not
    become the session's close, the archive's underlying or the moneyness reference."""
    prev = u.get("prev_close")
    price = u["rth_close"] if final and u.get("rth_close") else u["price"]
    change = price - prev if prev else None
    return Quote(symbol=symbol, ts=u.get("ts") or fetched_at, price=price, change=change,
                 change_pct=change / prev if change is not None else None, open=u.get("open"), high=u.get("high"),
                 low=u.get("low"), prev_close=prev, volume=None if u.get("volume") is None else int(u["volume"]),
                 bid=u.get("bid"), ask=u.get("ask"), iv30=None, delayed=delayed)


def _contract(symbol: str, c: dict) -> OptionContract:
    iv = c.get("iv")
    return OptionContract(
        osi=format_osi(symbol, c["expiry"], c["right"], c["strike"]), symbol=symbol, expiry=c["expiry"],
        strike=float(c["strike"]), right=c["right"], bid=c.get("bid"), ask=c.get("ask"), last=c.get("last"),
        change=None, volume=ctx.count(c.get("volume")), oi=ctx.count(c.get("oi")),
        iv=iv if iv and iv > 0 else None, delta=c.get("delta"), gamma=c.get("gamma"), theta=c.get("theta"),
        vega=c.get("vega"), rho=None, theo=None, last_ts=c.get("last_ts"))


def _with_computed_iv(quote: Quote, contracts: list[OptionContract], fetched: datetime,
                      source: str) -> list[OptionContract]:
    """Source IV/greeks labelled with `source`, missing ones computed (analytics.computed_iv) at the time the
    prices were current: fetched_at during the session, else the session's close (an evening or weekend
    snapshot holds the closing quotes), with the underlying's regular-session close then when stored."""
    day = session_date(fetched)
    close = session_close(day)
    asof = min(fetched, close)
    spot = quote.price
    if fetched >= close:
        b = bars_repo.get_bars(quote.symbol, start=day, end=day)
        if len(b["close"]) and ctx.num(b["close"][-1]):
            spot = float(b["close"][-1])
    r = ctx.risk_free_rate(day)
    q = ctx.dividend_yield(quote.symbol, spot, ChainArrays.from_contracts(contracts, spot, asof, symbol=quote.symbol))
    return computed_iv.fill_iv_greeks(contracts, spot=spot, asof=asof, r=r, q=q,
                                      session_start=datetime.combine(day, OPEN, NY), source=source)


def _check_final(day: date | None, fetched_at: datetime) -> date:
    """The session a closing snapshot belongs to: `day` must be given, be the session of fetched_at,
    and fetched_at must be at/after that session's close."""
    if day is None:
        raise BadRequest("session_date is required when final is true")
    if session_date(fetched_at) != day or fetched_at < session_close(day):
        raise BadRequest(f"fetched_at {fetched_at.isoformat()} is not after the close of session {day}")
    return day


def chain(body: dict, now: datetime | None = None) -> dict:
    """Store a pushed chain through jobs.chains.store_chain (latest chain + quote, session metrics;
    `final` also archives it as the end-of-day chain of `session_date`; source precedence is
    store_chain's). 404 for unknown symbols."""
    now = now or datetime.now(timezone.utc)
    sym = ctx.ensure_known(body["symbol"])["symbol"]
    fetched, source = body["fetched_at"], body["source"]
    if fetched > now + CLOCK_SKEW:
        raise BadRequest(f"fetched_at {fetched.isoformat()} is in the future")
    day = _check_final(body.get("session_date"), fetched) if body.get("final") else None
    quote = _quote(sym, body["underlying"], fetched, body["delayed"], day is not None)
    contracts = _with_computed_iv(quote, [_contract(sym, c) for c in body["contracts"]], fetched, source)
    snap = ChainSnapshot(quote, tuple(contracts), fetched, body["delayed"])
    chains.store_chain(snap, fetched, final=day is not None, source=source)
    if day is not None:
        ctx.invalidate()
    _sweep_done(session_date(fetched), [sym])
    bridge_repo.record_push(now)
    return {"stored": len(snap.contracts), "symbol": sym}


def no_options(body: dict, now: datetime | None = None) -> dict:
    """POST /api/ingest/no-options: the client's chain source (IBKR: no option parameters for the
    underlying) lists no options for the symbol -> optionable false, so the UI says so instead of
    waiting for a chain. A later stored chain sets it true again; stored data is kept. 404 for unknown
    symbols."""
    sym = ctx.ensure_known(body["symbol"])["symbol"]
    symbols_repo.set_optionable(sym, False)
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    return {"symbol": sym, "optionable": False}


# ----------------------------------------------------------------------------- series
def vol_history(kind: str, body: dict, now: datetime | None = None) -> dict:
    """Upsert a symbol's daily IV30/HV30 series (non-finite or non-positive values are skipped)."""
    sym = normalize_symbol(body["symbol"])
    points = [(b["date"], b["value"]) for b in body["bars"] if ctx.num(b["value"]) and b["value"] > 0]
    n = vol_history_repo.upsert(sym, kind, body["source"], points)
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    ctx.invalidate()   # IV rank caches
    return {"stored": n}


def index_bars(body: dict, now: datetime | None = None) -> dict:
    """Index daily bars (IBKR TRADES). The cash indices (SPX, NDX, RUT, XSP) are symbols: their pushed bars are their
    daily_bars, read by every price, return, range, HV and chart computation like any symbol's (replacing a bar the
    EOD job built from a quote). The VIX family is a market series: index_bars."""
    sym = normalize_symbol(body["symbol"])
    rows = [Bar(sym, b["date"], b.get("open"), b.get("high"), b.get("low"), b["close"], None) for b in body["bars"]]
    n = bars_repo.upsert_bars(rows) if sym in bridge_plan.EOD_INDICES else market_repo.upsert_index_bars(rows)
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    if sym in bridge_plan.EOD_INDICES:
        ctx.invalidate()
    return {"stored": n}


_QUOTE_FIELDS = ("price", "change", "change_pct", "bid", "ask", "open", "high", "low", "prev_close", "volume")


def quotes(body: dict, now: datetime | None = None) -> dict:
    """Upsert quotes field-wise: pushed fields replace the stored quote's, others are kept; a push
    older than the stored quote is ignored."""
    stored = quotes_repo.all_quotes()
    out = []
    for q in body["quotes"]:
        sym = normalize_symbol(q["symbol"])
        old = stored.get(sym)
        if old is not None and old.ts > q["ts"]:
            continue
        base: dict[str, Any] = {f: None for f in Quote.__slots__} if old is None else \
            {f: getattr(old, f) for f in Quote.__slots__}
        base.update(symbol=sym, ts=q["ts"], delayed=body["delayed"],
                    **{f: q.get(f) for f in _QUOTE_FIELDS if q.get(f) is not None})
        if q.get("volume") is not None:
            base["volume"] = int(q["volume"])
        out.append(Quote(**base))
    n = quotes_repo.upsert_quotes(out, body["source"]) if out else 0
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    return {"stored": n}


def underlying_stats(body: dict, now: datetime | None = None) -> dict:
    """Per-underlying option stats of a session -> that session's metrics rows (jobs.chains.
    store_underlying_stats: chain-derived values win; iv30 also feeds the IV history)."""
    stats = [{**st, "delayed": body["delayed"]} for st in body["stats"]]
    n = chains.store_underlying_stats(body["session_date"], stats, body["source"])
    _sweep_done(body["session_date"], [st["symbol"] for st in stats])
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    ctx.invalidate()
    return {"stored": n}


def option_bars(body: dict, now: datetime | None = None) -> dict:
    """Daily contract bars -> reconstructed end-of-day rows (jobs.catchup.store_reconstructed_bars:
    never over a real archive)."""
    n = catchup.store_reconstructed_bars(body["symbol"], body["bars"], body["source"])
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    return {"stored": n}


def ack_wants(body: dict, now: datetime | None = None) -> dict:
    """POST /api/ingest/ack-wants: the bridge accepted (queued) these wants of `kind`; only accepted
    option_bars offers count towards bridge_plan.MAX_OFFERS."""
    n = bridge_plan.record_offers([{"symbol": normalize_symbol(i["symbol"]), "sessions": i["sessions"]}
                                   for i in body["items"]], now)
    return {"recorded": n}


def heartbeat(body: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    bridge_repo.record_heartbeat(body["client"], body["ibkr_connected"], body.get("account_type"), now)
    return {"server_time": now}


# ----------------------------------------------------------------------------- wants
def wants(now: datetime | None = None) -> dict:
    """GET /api/ingest/wants: IV history for watchlist + index + universe symbols whose ingested
    history is missing or ends before the last completed session (from the day after it, else
    IV_HISTORY_YEARS back), plus the plans of jobs.bridge_plan (underlying stats, chain universe,
    closing chains, option-bar catch-up), the VIX family (quotes), missing index history (index_bars_want) and the
    on-demand chain freshness bound."""
    now = (now or now_ny()).astimezone(NY)
    last = last_completed_session(now)
    syms = list(dict.fromkeys([*user_repo.watchlist_symbols(), *bridge_plan.EOD_INDICES,
                               *symbols_repo.universe_symbols()]))
    have = vol_history_repo.last_dates("iv30", syms)
    start = date(now.year - IV_HISTORY_YEARS, 1, 1)
    return {
        "iv_history": [{"symbol": s, "from": have[s] + timedelta(days=1) if s in have else start}
                       for s in syms if s not in have or have[s] < last],
        "underlying_stats": bridge_plan.underlying_stats_want(now),
        "chain_universe": bridge_plan.chain_universe_want(now),
        "eod_chains": bridge_plan.eod_chains_want(now),
        "option_bars": bridge_plan.option_bars_want(now),
        "index_symbols": list(VIX_FAMILY),
        "index_bars": index_bars_want(now.date(), last),
        "chains_on_demand_max_age_s": int(ctx.REFRESH_MIN_INTERVAL_S),
    }


INDEX_HISTORY_YEARS = 10
INDEX_COVER_SLACK_DAYS = 30   # a history starting this much after the wanted start is refetched in full


def index_bars_want(today: date, last_session: date) -> list[dict]:
    """Index daily history the bridge should fetch (IBKR TRADES): the cash indices (daily_bars) and the VIX family
    (index_bars) without a 10-year history (from 10 years back), else from the day after their last bar when that
    predates the last completed session."""
    start = today - timedelta(days=365 * INDEX_HISTORY_YEARS)
    cov = {**bars_repo.coverage(list(bridge_plan.EOD_INDICES)), **market_repo.index_coverage(VIX_FAMILY)}
    out = []
    for s in (*bridge_plan.EOD_INDICES, *VIX_FAMILY):
        c = cov.get(s)
        if c is None or c["first"] > start + timedelta(days=INDEX_COVER_SLACK_DAYS):
            out.append({"symbol": s, "from": start})
        elif c["last"] < last_session:
            out.append({"symbol": s, "from": c["last"] + timedelta(days=1)})
    return out
