"""What the server wants a data client (the IBKR bridge) to fetch: the parts of GET /api/ingest/wants
(docs/INTEGRATION.md §6) that depend on stored data. Plain values out; the API layer serializes.

The bridge serves every market OptionLens follows (analytics/markets.py). Work that depends on the trading session
(underlying stats, the chain universe, closing chains, option-bar catch-up) is planned per market, each with its own
calendar: on a US-only holiday the Canadian plans run a live session while the US ones are an after-hours fill.

- chain_universe: watchlists + symbols opened recently + the most liquid N by stored option volume
  (N = settings.chain_universe_size), wanted from 09:00 to 20:00 ET on trading days. Reports that need
  full chains cover only these symbols.
- underlying_stats: the whole universe, during the session (open .. eod_deadline).
- after-hours fills: outside those hours both are wanted for the symbols whose data of the current
  session (session_date: the last completed one in the evening, on weekends and before the open) is
  missing, marked `as_of_session` — stats when more than STATS_FILL_MIN_MISSING of the universe has no
  metrics row for it, chains for chain-universe symbols without a chain fetched since its open. So a first
  launch on a weekend or in the evening populates the last session from IBKR's frozen data
  (docs/INTEGRATION.md §6 "After-hours fills"), watchlists first, then recently opened, then most liquid.
- eod_chains: chain universe + indices whose session has no real-time closing archive yet.
- Splitting chain work (chain_universe, its after-hours fill, eod_chains): while a server-side source (Questrade)
  is healthy — available (connected, not needing attention) and not failing (jobs.source_health) — it sweeps the
  whole universe itself, so the bridge is asked only for the symbols that source did not cover: its last attempt
  failed, or it has no chain of the symbol (it lists no options for it or does not list it: another source may,
  jobs/optionable.py). When no server-side source is healthy the bridge gets the full lists.
  IV/HV history, underlying stats, index data and option-bar catch-up stay the bridge's (only IBKR provides them).
- option_bars: chain-universe symbols with recent sessions lacking an end-of-day archive (catch-up);
  each (session, symbol) is offered until the bridge has accepted it MAX_OFFERS times (POST
  /api/ingest/ack-wants, record_offers), and again only REOFFER_AFTER after an accepted offer.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..analytics import markets
from ..analytics.calendar import NY, OPEN, now_ny
from ..config import get_settings
from ..repo import bars as bars_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import symbols as symbols_repo
from ..repo import user as user_repo
from ..repo import views as views_repo
from ..sources import chain_source
from . import catchup, optionable, source_health
from ._util import eod_deadline, final_since, session_date

EOD_INDICES = ("SPX", "NDX", "RUT", "XSP")
RECENT_VIEW_DAYS = 7
CHAIN_MAX_AGE_S = 3600
CHAIN_EXPIRIES = 6
CHAIN_MONEYNESS = 0.20
STATS_MAX_AGE_S = 1800
CHAIN_HOURS = (time(9, 0), time(20, 0))   # ET, the market's trading days: pre-market until the closing-chain deadline
MAX_OFFERS = 3
REOFFER_AFTER = timedelta(hours=6)
FILL_MAX_AGE_S = 6 * 3600       # an after-hours fill is pushed once; a symbol IBKR sent nothing for waits this long
STATS_FILL_MIN_MISSING = 0.20   # share of the universe without a metrics row that makes a session "uncovered"


def _unique(*groups) -> list[str]:
    return list(dict.fromkeys(s for g in groups for s in g))


def of_market(market: markets.Market, symbols) -> list[str]:
    """Of `symbols`, those of `market` (same order)."""
    return [s for s in symbols if markets.market_of(s) is market]


def _user_first(now: datetime) -> list[str]:
    """Watchlist symbols, then symbols opened in the last RECENT_VIEW_DAYS."""
    return _unique(user_repo.watchlist_symbols(), views_repo.viewed_since(now - timedelta(days=RECENT_VIEW_DAYS)))


def chain_universe(now: datetime | None = None) -> list[str]:
    """The bridge's chain universe (every market): watchlist symbols, then symbols opened in the last
    RECENT_VIEW_DAYS, then the top
    settings.chain_universe_size symbols by mean option volume of their last 5 stored sessions; without the
    symbols the bridge itself answered list no options (jobs/optionable.py; another source's answer does not count;
    opening one still asks the bridge on demand)."""
    now = now or now_ny()
    n = getattr(get_settings(), "chain_universe_size", 150)
    syms = _unique(_user_first(now), metrics_repo.top_by_option_volume(n) if n else [])
    none = optionable.answered_none(chain_source.BRIDGE_TAG, syms)
    return [s for s in syms if s not in none]


def prioritized(symbols: list[str], now: datetime) -> list[str]:
    """`symbols` in fill order: watchlists, recently opened, most liquid (stored option volume), the rest."""
    want = set(symbols)
    first = [s for s in _unique(_user_first(now), metrics_repo.top_by_option_volume(len(symbols), symbols=symbols))
             if s in want]
    return _unique(first, symbols)


def server_covered(symbols: list[str]) -> set[str]:
    """Of `symbols`, those a healthy server-side source sweeps and has a chain of: its last attempt stored one, or it
    was not asked yet (the sweep will) and has not answered "none" before (see module doc "Splitting chain work")."""
    out: set[str] = set()
    for src in chain_source.server_sources():
        if src.unavailable() is not None or source_health.failing(src.name):
            continue
        none = optionable.answered_none(src.name, symbols)
        for s in symbols:
            a = source_health.last(src.name, s)
            if (a is None and s not in none) or (a is not None and a.result == "ok"):
                out.add(s)
    return out


def for_bridge(symbols: list[str]) -> list[str]:
    """`symbols` minus those a healthy server-side source covers (same order)."""
    covered = server_covered(symbols)
    return [s for s in symbols if s not in covered]


def chain_universe_want(now: datetime | None = None, market: markets.Market = markets.US) -> dict:
    """`market`'s part of the chain universe from CHAIN_HOURS[0] to CHAIN_HOURS[1] ET on its trading days. Outside
    those hours quotes do not move, so only an after-hours fill (`as_of_session`): the symbols without a chain
    fetched since the open of the market's current session (session_date), once each (FILL_MAX_AGE_S). Both without
    the symbols a healthy server-side source covers (for_bridge)."""
    now = (now or now_ny()).astimezone(NY)
    cal = market.calendar
    shape = {"market": market.code, "expiries": CHAIN_EXPIRIES, "moneyness": CHAIN_MONEYNESS}
    syms = for_bridge(of_market(market, chain_universe(now)))
    if cal.is_trading_day(now.date()) and CHAIN_HOURS[0] <= now.time() < CHAIN_HOURS[1]:
        return {"symbols": syms, "max_age_s": CHAIN_MAX_AGE_S, **shape}
    day = session_date(now, cal)
    have = options_repo.fetched_since(datetime.combine(day, OPEN, NY), syms)
    return {"symbols": [s for s in syms if s not in have], "max_age_s": FILL_MAX_AGE_S, **shape, "as_of_session": day}


def underlying_stats_want(now: datetime | None = None, market: markets.Market = markets.US) -> dict:
    """`market`'s universe while its session of today runs (open until its eod_deadline), `session_date` = today.
    Otherwise an after-hours fill (`as_of_session` = `session_date` = the market's session_date) when the session is
    uncovered — more than STATS_FILL_MIN_MISSING of that universe has no metrics row for it: the symbols without
    one, in `prioritized` order."""
    now = (now or now_ny()).astimezone(NY)
    cal = market.calendar
    d = now.date()
    universe = of_market(market, symbols_repo.universe_symbols())
    if cal.is_trading_day(d) and datetime.combine(d, OPEN, NY) <= now < eod_deadline(d):
        return {"market": market.code, "session_date": d, "symbols": universe, "max_age_s": STATS_MAX_AGE_S}
    day = session_date(now, cal)
    have = metrics_repo.symbols_on(day)
    missing = [s for s in universe if s not in have]
    uncovered = len(missing) > STATS_FILL_MIN_MISSING * len(universe)
    return {"market": market.code, "session_date": day, "symbols": prioritized(missing, now) if uncovered else [],
            "max_age_s": FILL_MAX_AGE_S, "as_of_session": day}


def eod_chains_want(now: datetime | None = None, market: markets.Market = markets.US) -> dict:
    """Closing chains of `market`'s current session (its chain-universe symbols; US: + the indices) without a
    real-time closing archive yet (a closing chain of IBKR's could still win, jobs/precedence.py) and not covered by
    a healthy server-side source (for_bridge), wanted from the market's final_since (its close + 20 min) until
    eod_deadline (20:00 ET)."""
    now = now or now_ny()
    cal = market.calendar
    day = session_date(now, cal)
    have = options_repo.eod_quality(day)

    def settled(s: str) -> bool:
        a = have.get(s)
        return a is not None and a["delayed"] is False
    wanted = of_market(market, _unique(chain_universe(now), EOD_INDICES))
    return {"market": market.code, "session_date": day,
            "symbols": for_bridge([s for s in wanted if not settled(s)]),
            "not_before": final_since(day, cal), "deadline": eod_deadline(day)}


def option_bars_want(now: datetime | None = None) -> list[dict]:
    """[{symbol, sessions, expiries, moneyness}] for chain-universe symbols whose recent sessions
    (catchup.catchup_sessions of the symbol's market) have no archive and whose underlying close for them is stored
    (needed for IV). A (session, symbol) the bridge accepted (record_offers) is re-offered only REOFFER_AFTER
    later, and not after MAX_OFFERS accepted offers (a session the bridge has no bars for is not asked
    for forever). Reading the want records nothing."""
    now = now or now_ny()
    out = []
    for market, syms in markets.by_market(s for s in chain_universe(now) if s not in EOD_INDICES).items():
        out += _option_bars_want(now, syms, catchup.catchup_sessions(now, cal=market.calendar))
    return out


def _option_bars_want(now: datetime, syms: list[str], sessions: list[date]) -> list[dict]:
    missing = catchup.missing_eod(syms, sessions)
    if not missing:
        return []
    last_bar = {s: c["last"] for s, c in bars_repo.coverage(list(missing)).items()}
    offers = options_repo.catchup_offers(sessions)

    def due(d: date, s: str) -> bool:
        prev = offers.get((d, s))
        return prev is None or (prev[1] < MAX_OFFERS and now - prev[0] >= REOFFER_AFTER)

    out = []
    for s, days in missing.items():
        days = [d for d in days if s in last_bar and d <= last_bar[s] and due(d, s)]
        if days:
            out.append({"symbol": s, "sessions": days, "expiries": catchup.EXPIRIES,
                        "moneyness": catchup.MONEYNESS})
    return out


def record_offers(items: list[dict], now: datetime | None = None) -> int:
    """The bridge accepted these option_bars wants ([{symbol, sessions}]): count one offer of each
    (session, symbol). Returns how many were counted."""
    pairs = list(dict.fromkeys((d, s["symbol"]) for s in items for d in s["sessions"]))
    options_repo.record_catchup_offers(pairs, now or now_ny())
    return len(pairs)
