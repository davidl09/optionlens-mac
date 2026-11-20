"""Ingest API (docs/INTEGRATION.md §6): data pushed by a client (the IBKR bridge) stored through the
same pipeline as the server's own collectors, and `wants` — what the server would like it to fetch.

Inputs are validated plain values (the API layer parses JSON); every write is an idempotent upsert.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..analytics.calendar import NY, last_completed_session, now_ny
from ..jobs import bridge_plan, catchup, chains, hooks
from ..jobs._util import session_close, session_date
from ..jobs.marketdata import VIX_FAMILY
from ..models import Bar, ChainSnapshot, OptionContract, Quote, normalize_symbol
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
def _quote(symbol: str, u: dict, fetched_at: datetime, delayed: bool) -> Quote:
    price, prev = u["price"], u.get("prev_close")
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
        change=None, volume=int(c.get("volume") or 0), oi=int(c.get("oi") or 0),
        iv=iv if iv and iv > 0 else None, delta=c.get("delta"), gamma=c.get("gamma"), theta=c.get("theta"),
        vega=c.get("vega"), rho=None, theo=None)


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
    snap = ChainSnapshot(_quote(sym, body["underlying"], fetched, body["delayed"]),
                         tuple(_contract(sym, c) for c in body["contracts"]), fetched, body["delayed"])
    chains.store_chain(snap, fetched, final=day is not None, source=source)
    if day is not None:
        ctx.invalidate()
    _sweep_done(session_date(fetched), [sym])
    bridge_repo.record_push(now)
    return {"stored": len(snap.contracts), "symbol": sym}


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
    sym = normalize_symbol(body["symbol"])
    n = market_repo.upsert_index_bars(Bar(sym, b["date"], b.get("open"), b.get("high"), b.get("low"), b["close"],
                                          None) for b in body["bars"])
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    return {"stored": n}


_QUOTE_FIELDS = ("price", "change", "change_pct")


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
    closing chains, option-bar catch-up), the VIX family and the on-demand chain freshness bound."""
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
        "chains_on_demand_max_age_s": int(ctx.REFRESH_MIN_INTERVAL_S),
    }
