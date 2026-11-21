"""What the server wants a data client (the IBKR bridge) to fetch: the parts of GET /api/ingest/wants
(docs/INTEGRATION.md §6) that depend on stored data. Plain values out; the API layer serializes.

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
- eod_chains: chain universe + indices whose closing chain for the session has not arrived from IBKR.
- option_bars: chain-universe symbols with recent sessions lacking an end-of-day archive (catch-up);
  each (session, symbol) is offered until the bridge has accepted it MAX_OFFERS times (POST
  /api/ingest/ack-wants, record_offers), and again only REOFFER_AFTER after an accepted offer.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..analytics.calendar import NY, OPEN, is_trading_day, now_ny
from ..config import get_settings
from ..repo import bars as bars_repo
from ..repo import metrics as metrics_repo
from ..repo import options as options_repo
from ..repo import symbols as symbols_repo
from ..repo import user as user_repo
from ..repo import views as views_repo
from . import catchup
from ._util import eod_deadline, final_since, session_date

EOD_INDICES = ("SPX", "NDX", "RUT", "XSP")
RECENT_VIEW_DAYS = 7
CHAIN_MAX_AGE_S = 3600
CHAIN_EXPIRIES = 6
CHAIN_MONEYNESS = 0.20
STATS_MAX_AGE_S = 1800
CHAIN_HOURS = (time(9, 0), time(20, 0))   # ET, trading days: pre-market until the closing-chain deadline
MAX_OFFERS = 3
REOFFER_AFTER = timedelta(hours=6)
FILL_MAX_AGE_S = 6 * 3600       # an after-hours fill is pushed once; a symbol IBKR sent nothing for waits this long
STATS_FILL_MIN_MISSING = 0.20   # share of the universe without a metrics row that makes a session "uncovered"


def _unique(*groups) -> list[str]:
    return list(dict.fromkeys(s for g in groups for s in g))


def _user_first(now: datetime) -> list[str]:
    """Watchlist symbols, then symbols opened in the last RECENT_VIEW_DAYS."""
    return _unique(user_repo.watchlist_symbols(), views_repo.viewed_since(now - timedelta(days=RECENT_VIEW_DAYS)))


def chain_universe(now: datetime | None = None) -> list[str]:
    """Watchlist symbols, then symbols opened in the last RECENT_VIEW_DAYS, then the top
    settings.chain_universe_size symbols by mean option volume of their last 5 stored sessions; without
    symbols a chain source confirmed list no options (opening one still asks the bridge on demand)."""
    now = now or now_ny()
    n = getattr(get_settings(), "chain_universe_size", 150)
    none = {r["symbol"] for r in symbols_repo.list_symbols(optionable=False)}
    return [s for s in _unique(_user_first(now), metrics_repo.top_by_option_volume(n) if n else []) if s not in none]


def prioritized(symbols: list[str], now: datetime) -> list[str]:
    """`symbols` in fill order: watchlists, recently opened, most liquid (stored option volume), the rest."""
    want = set(symbols)
    first = [s for s in _unique(_user_first(now), metrics_repo.top_by_option_volume(len(symbols), symbols=symbols))
             if s in want]
    return _unique(first, symbols)


def chain_universe_want(now: datetime | None = None) -> dict:
    """The chain universe from CHAIN_HOURS[0] to CHAIN_HOURS[1] ET on trading days. Outside those hours quotes
    do not move, so only an after-hours fill (`as_of_session`): the chain-universe symbols without a chain
    fetched since the open of the current session (session_date), once each (FILL_MAX_AGE_S)."""
    now = (now or now_ny()).astimezone(NY)
    shape = {"expiries": CHAIN_EXPIRIES, "moneyness": CHAIN_MONEYNESS}
    if is_trading_day(now.date()) and CHAIN_HOURS[0] <= now.time() < CHAIN_HOURS[1]:
        return {"symbols": chain_universe(now), "max_age_s": CHAIN_MAX_AGE_S, **shape}
    day = session_date(now)
    syms = chain_universe(now)
    have = options_repo.fetched_since(datetime.combine(day, OPEN, NY), syms)
    return {"symbols": [s for s in syms if s not in have], "max_age_s": FILL_MAX_AGE_S, **shape, "as_of_session": day}


def underlying_stats_want(now: datetime | None = None) -> dict:
    """The universe while today's session runs (open until its eod_deadline). Otherwise an after-hours fill
    (`as_of_session` = session_date) when the session is uncovered — more than STATS_FILL_MIN_MISSING of the
    universe has no metrics row for it: the symbols without one, in `prioritized` order."""
    now = (now or now_ny()).astimezone(NY)
    d = now.date()
    universe = symbols_repo.universe_symbols()
    if is_trading_day(d) and datetime.combine(d, OPEN, NY) <= now < eod_deadline(d):
        return {"symbols": universe, "max_age_s": STATS_MAX_AGE_S}
    day = session_date(now)
    have = metrics_repo.symbols_on(day)
    missing = [s for s in universe if s not in have]
    uncovered = len(missing) > STATS_FILL_MIN_MISSING * len(universe)
    return {"symbols": prioritized(missing, now) if uncovered else [], "max_age_s": FILL_MAX_AGE_S, "as_of_session": day}


def eod_chains_want(now: datetime | None = None) -> dict:
    """Closing chains of the current session not yet received from IBKR (chain universe + indices),
    wanted from final_since (close + 20 min) until eod_deadline (20:00 ET)."""
    now = now or now_ny()
    day = session_date(now)
    have = options_repo.eod_sources([day])[day]
    return {"session_date": day,
            "symbols": [s for s in _unique(chain_universe(now), EOD_INDICES) if have.get(s) != options_repo.IBKR],
            "not_before": final_since(day), "deadline": eod_deadline(day)}


def option_bars_want(now: datetime | None = None) -> list[dict]:
    """[{symbol, sessions, expiries, moneyness}] for chain-universe symbols whose recent sessions
    (catchup.catchup_sessions) have no archive and whose underlying close for them is stored (needed
    for IV). A (session, symbol) the bridge accepted (record_offers) is re-offered only REOFFER_AFTER
    later, and not after MAX_OFFERS accepted offers (a session the bridge has no bars for is not asked
    for forever). Reading the want records nothing."""
    now = now or now_ny()
    syms = [s for s in chain_universe(now) if s not in EOD_INDICES]
    sessions = catchup.catchup_sessions(now)
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
