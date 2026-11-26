"""Ingest API (docs/INTEGRATION.md §6): data pushed by a client (the IBKR bridge) stored through the
same pipeline as the server's own collectors, and `wants` — what the server would like it to fetch.

Inputs are validated plain values (the API layer parses JSON); every write is an idempotent upsert.
Every session rule here (closing snapshots, the session of stats, history wants) uses the calendar of the symbol's
own market (analytics.markets): the bridge serves US and Canadian listings alike.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..analytics import computed_iv, markets
from ..analytics import dividends as divs_a
from ..analytics.calendar import NY, OPEN, TradingCalendar, last_completed_session, now_ny
from ..analytics.chain import ChainArrays
from ..jobs import bridge_plan, catchup, chains, hooks, optionable
from ..jobs._util import eod_deadline, session_close, session_date
from ..jobs.marketdata import VIX_FAMILY
from ..models import Bar, ChainSnapshot, Dividend, OptionContract, Quote, normalize_symbol
from ..repo import bars as bars_repo
from ..repo import bridge as bridge_repo
from ..repo import dividends as div_repo
from ..repo import market as market_repo
from ..repo import options as options_repo
from ..repo import quotes as quotes_repo
from ..repo import symbols as symbols_repo
from ..repo import user as user_repo
from ..repo import vol_history as vol_history_repo
from ..sources.osi import format_osi
from .. import live
from . import context as ctx
from .errors import BadRequest, NotFound

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


def complete_chain(snap: ChainSnapshot, source: str) -> ChainSnapshot:
    """`snap` with source IV/greeks labelled `source`, a contract's missing IV taken from another source's chain of
    the same session when it has one (computed_iv.borrow_source_iv), the rest computed (analytics.computed_iv) at
    the time the prices were current: fetched_at during the session, else the session's close (an evening or
    weekend snapshot holds the closing quotes), with the underlying's regular-session close then when stored.
    Used for pushed chains and, as a jobs.hooks.chain_preparers entry, for server-side sources' chains."""
    quote, fetched = snap.quote, snap.fetched_at
    cal = markets.calendar_for(quote.symbol)
    day = session_date(fetched, cal)
    close = session_close(day, cal)
    asof = min(fetched, close)
    session_start = datetime.combine(day, OPEN, NY)
    spot = quote.price
    if fetched >= close:
        b = bars_repo.get_bars(quote.symbol, start=day, end=day)
        if len(b["close"]) and ctx.num(b["close"][-1]):
            spot = float(b["close"][-1])
    contracts = computed_iv.borrow_source_iv(snap.contracts, options_repo.latest_source_iv(quote.symbol, session_start),
                                             source)
    r = ctx.rate_for(quote.symbol, day)
    q = ctx.dividend_yield(quote.symbol, spot, ChainArrays.from_contracts(contracts, spot, asof, symbol=quote.symbol))
    done = computed_iv.fill_iv_greeks(contracts, spot=spot, asof=asof, r=r, q=q, session_start=session_start,
                                      source=source)
    return dataclasses.replace(snap, contracts=tuple(done))


def register_hooks() -> None:
    """Complete server-side sources' chains like pushed ones (idempotent; call once at startup)."""
    if complete_chain not in hooks.chain_preparers:
        hooks.chain_preparers.append(complete_chain)


def _check_final(day: date | None, fetched_at: datetime, cal: TradingCalendar) -> date:
    """The session a closing snapshot belongs to: `day` must be given, be the session of fetched_at in the symbol's
    market (`cal`), and fetched_at must be at/after that session's close."""
    if day is None:
        raise BadRequest("session_date is required when final is true")
    if session_date(fetched_at, cal) != day or fetched_at < session_close(day, cal):
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
    cal = markets.calendar_for(sym)
    day = _check_final(body.get("session_date"), fetched, cal) if body.get("final") else None
    quote = _quote(sym, body["underlying"], fetched, body["delayed"], day is not None)
    listed = body.get("contracts_listed_at") if body.get("contracts_from_cache") else None
    if body.get("contracts_from_cache") and listed is None:
        raise BadRequest("contracts_listed_at is required when contracts_from_cache is true")
    snap = complete_chain(ChainSnapshot(quote, tuple(_contract(sym, c) for c in body["contracts"]), fetched,
                                        body["delayed"], listed), source)
    chains.store_chain(snap, fetched, final=day is not None, source=source,
                       reference_quote=bool(body["underlying"].get("reference")))
    if day is not None:
        ctx.invalidate()
    _sweep_done(session_date(fetched, cal), [sym])
    bridge_repo.record_push(now)
    return {"stored": len(snap.contracts), "symbol": sym}


def reference_price(symbol: str) -> dict:
    """GET /api/ingest/reference-price/{symbol} (docs/INTEGRATION.md §6 "Reference price"): the engine's own current
    price of a symbol (context.quote_dict: the newest stored quote, else the latest daily close), for a client that got
    no price of the underlying from its feed (IBKR without that market-data subscription, e.g. a CBOE index) and still
    needs a strike band for the chain. -> {symbol, price, ts, source}; 404 when none is stored or for unknown symbols."""
    sym = ctx.ensure_known(symbol)["symbol"]
    q = ctx.quote_dict(sym)
    if q is None or not q.get("price"):
        raise NotFound(f"no price stored for {sym}")
    return {"symbol": sym, "price": q["price"], "ts": q["ts"], "source": q["source"]}


def no_options(body: dict, now: datetime | None = None) -> dict:
    """POST /api/ingest/no-options: the client's chain source (IBKR: no option parameters for the
    underlying) lists no options for the symbol. Recorded as that source's answer (jobs/optionable.py):
    optionable becomes false once every chain source serving the symbol answered so, and the UI then says
    so instead of waiting for a chain. A later stored chain sets it true again; stored data is kept.
    -> {symbol, optionable (true / false / null = not known yet)}; 404 for unknown symbols."""
    sym = ctx.ensure_known(body["symbol"])["symbol"]
    flag = optionable.confirm_none(sym, body["source"], "no_options")
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    return {"symbol": sym, "optionable": flag}


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


def quotes(body: dict, now: datetime | None = None) -> dict:
    """Upsert quotes field-wise (quotes_repo.merge_quotes): pushed fields replace the stored quote's, others are
    kept; a push older than the stored quote is ignored. Live subscribers get them too (live.hub)."""
    rows = [{**q, "symbol": normalize_symbol(q["symbol"])} for q in body["quotes"]]
    n = quotes_repo.merge_quotes(rows, body["source"], body["delayed"])
    live.instance().ingested_quotes(body["source"], body["delayed"], rows)
    bridge_repo.record_push(now or datetime.now(timezone.utc))
    return {"stored": n}


def underlying_stats(body: dict, now: datetime | None = None) -> dict:
    """Per-underlying option stats of a session -> that session's metrics rows (jobs.chains.
    store_underlying_stats: chain-derived values win; iv30 also feeds the IV history). One push holds symbols of
    one market (400 otherwise: markets have different sessions), and `session_date` must be that market's
    session of `ts`; stats collected after that session's stats window (eod_deadline: evening, weekend,
    next pre-market) are an after-hours fill."""
    day, ts = body["session_date"], body["ts"]
    if ts > (now or datetime.now(timezone.utc)) + CLOCK_SKEW:
        raise BadRequest(f"ts {ts.isoformat()} is in the future")
    found = list(markets.by_market(st["symbol"] for st in body["stats"]))
    if len(found) > 1:
        raise BadRequest("stats of one push must be of one market (" + ", ".join(m.code for m in found) + " sent)")
    of_ts = session_date(ts, found[0].calendar if found else markets.US.calendar)
    if of_ts != day:
        raise BadRequest(f"stats collected at {ts.isoformat()} are not of session {day} (they are of {of_ts})")
    stats = [{**st, "delayed": body["delayed"]} for st in body["stats"]]
    n = chains.store_underlying_stats(day, stats, body["source"], after_hours=ts >= eod_deadline(day))
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


DERIVED_SOURCE = "ibkr-adjusted"


def dividend_history(body: dict, now: datetime | None = None) -> dict:
    """POST /api/ingest/dividend-history: IBKR's dividend-adjusted (ADJUSTED_LAST) and raw (TRADES) daily closes of a
    symbol -> the dividends behind the adjustment (analytics.dividends.from_adjusted_closes), stored with source
    'ibkr-adjusted' over the span the closes cover (replacing what an earlier fetch derived there), and the fetch
    recorded (also when nothing was paid) so dividend_history_want does not ask again for a while."""
    now = now or datetime.now(timezone.utc)
    sym = normalize_symbol(body["symbol"])
    raw = [(b["date"], b["close"]) for b in body["trades"]]
    adjusted = [(b["date"], b["close"]) for b in body["adjusted"]]
    common = sorted({d for d, _ in raw} & {d for d, _ in adjusted})
    found = [Dividend(sym, d, amt, None, None, None, "cash", source=DERIVED_SOURCE)
             for d, amt in divs_a.from_adjusted_closes(raw, adjusted)]
    n = div_repo.replace_derived(sym, DERIVED_SOURCE, common[1], common[-1], found, now) if len(common) > 1 else 0
    div_repo.record_history_fetch(sym, DERIVED_SOURCE, common[0] if common else None,
                                  common[-1] if common else None, n, now)
    bridge_repo.record_push(now)
    ctx.invalidate()   # dividend yields (q) and the universe frame
    return {"stored": n, "symbol": sym}


def ack_wants(body: dict, now: datetime | None = None) -> dict:
    """POST /api/ingest/ack-wants: the bridge accepted (queued) these wants of `kind`; only accepted
    option_bars offers count towards bridge_plan.MAX_OFFERS."""
    n = bridge_plan.record_offers([{"symbol": normalize_symbol(i["symbol"]), "sessions": i["sessions"]}
                                   for i in body["items"]], now)
    return {"recorded": n}


def heartbeat(body: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    bridge_repo.record_heartbeat(body["client"], body["ibkr_connected"], body.get("account_type"), now,
                                 farms=body.get("farms"), options_subscription=body.get("options_subscription"))
    return {"server_time": now}


# ----------------------------------------------------------------------------- wants
def wants(now: datetime | None = None) -> dict:
    """GET /api/ingest/wants (WANTS_VERSION): IV and HV history for watchlist + index + universe symbols of every
    market (history_want, each up to its own market's last completed session;
    `history_symbols` = how many symbols that is, so the client can show progress over all of them), plus the plans
    of jobs.bridge_plan — underlying stats, chain universe and closing chains as one plan per market (each on its
    market's calendar), option-bar catch-up —, the VIX family (quotes), missing index history (index_bars_want) and
    the on-demand chain freshness bound."""
    now = (now or now_ny()).astimezone(NY)
    by_market = markets.by_market(dict.fromkeys([*user_repo.watchlist_symbols(), *bridge_plan.EOD_INDICES,
                                                 *symbols_repo.universe_symbols()]))
    start = date(now.year - IV_HISTORY_YEARS, 1, 1)

    def history(kind: str) -> list[dict]:
        return [x for m, syms in by_market.items()
                for x in history_want(kind, syms, start, m.calendar.last_completed_session(now))]
    plans = list(markets.MARKETS.values())
    return {
        "version": WANTS_VERSION,
        "iv_history": history("iv30"),
        "hv_history": history("hv30"),
        "history_symbols": sum(len(syms) for syms in by_market.values()),
        "underlying_stats": [bridge_plan.underlying_stats_want(now, m) for m in plans],
        "chain_universe": [bridge_plan.chain_universe_want(now, m) for m in plans],
        "eod_chains": [bridge_plan.eod_chains_want(now, m) for m in plans],
        "option_bars": bridge_plan.option_bars_want(now),
        "index_symbols": list(VIX_FAMILY),
        "index_bars": index_bars_want(now.date(), last_completed_session(now)),
        "dividend_history": dividend_history_want(now),
        "chains_on_demand_max_age_s": int(ctx.REFRESH_MIN_INTERVAL_S),
    }


# The shape of GET /api/ingest/wants (docs/INTEGRATION.md §6). 2: `underlying_stats`, `chain_universe` and
# `eod_chains` are lists of per-market plans (each with `market`; stats plans carry their `session_date`).
WANTS_VERSION = 2
HISTORY_MIN_SPAN_DAYS = 30   # a series spanning less was not backfilled (a few single-day values)


def history_want(kind: str, symbols: list[str], start: date, last_session: date) -> list[dict]:
    """[{symbol, from}] of a vol_history kind to fetch: from `start` (IV_HISTORY_YEARS back) for a symbol without a
    backfilled series — none, or one spanning under HISTORY_MIN_SPAN_DAYS (e.g. only the IV30 values of underlying
    stats pushes, which are single days and do not count) — else from the day after its last point when that
    predates the last completed session. A symbol whose history is current is not listed."""
    cov = vol_history_repo.history_coverage(kind, symbols, chains.STATS_SUFFIX)
    out = []
    for s in symbols:
        c = cov.get(s)
        if c is None or (c[1] - c[0]).days < HISTORY_MIN_SPAN_DAYS:
            out.append({"symbol": s, "from": start})
        elif c[1] < last_session:
            out.append({"symbol": s, "from": c[1] + timedelta(days=1)})
    return out


DIVIDEND_HISTORY_YEARS = 10
DIVIDEND_REFRESH_DAYS = 7     # a symbol's dividend history is fetched again this long after the last fetch ...
DIVIDEND_OVERLAP_DAYS = 10    # ... from this many days before the last close it covered (the step needs the day before)


def dividend_history_want(now: datetime) -> list[dict]:
    """[{symbol, from}] the bridge should fetch daily adjusted + raw closes for (dividends of every listing, US or
    Canadian, docs/INTEGRATION.md §6 "Dividends"): watchlist and universe stocks / ETFs never fetched (from
    DIVIDEND_HISTORY_YEARS back) or last fetched DIVIDEND_REFRESH_DAYS ago (from DIVIDEND_OVERLAP_DAYS before the
    last close covered); watchlist symbols first."""
    rows = {r["symbol"]: r for r in symbols_repo.list_symbols()}
    syms = [s for s in dict.fromkeys([*user_repo.watchlist_symbols(),
                                      *symbols_repo.universe_symbols(include_unoptionable=True)])
            if s in rows and rows[s]["kind"] in ("stock", "etf")]
    done = div_repo.history_fetches(syms)
    start = date(now.year - DIVIDEND_HISTORY_YEARS, now.month, 1)
    out = []
    for s in syms:
        f = done.get(s)
        if f is not None and now - f["fetched_at"] < timedelta(days=DIVIDEND_REFRESH_DAYS):
            continue
        last = f["last_date"] if f is not None else None
        out.append({"symbol": s, "from": start if last is None else last - timedelta(days=DIVIDEND_OVERLAP_DAYS)})
    return out


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
