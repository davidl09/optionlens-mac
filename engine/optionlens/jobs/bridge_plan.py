"""What the server wants a data client (the IBKR bridge) to fetch: the parts of GET /api/ingest/wants
(docs/INTEGRATION.md §6) that depend on stored data. Plain values out; the API layer serializes.

- chain_universe: watchlists + symbols opened recently + the most liquid N by stored option volume
  (N = settings.chain_universe_size). Reports that need full chains cover only these symbols.
- underlying_stats: the whole universe, during the session (open .. eod_deadline).
- eod_chains: chain universe + indices whose closing chain for the session has not arrived from IBKR.
- option_bars: chain-universe symbols with recent sessions lacking an end-of-day archive (catch-up);
  each (session, symbol) is offered at most MAX_OFFERS times, REOFFER_AFTER apart.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

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
MAX_OFFERS = 3
REOFFER_AFTER = timedelta(hours=6)


def _unique(*groups) -> list[str]:
    return list(dict.fromkeys(s for g in groups for s in g))


def chain_universe(now: datetime | None = None) -> list[str]:
    """Watchlist symbols, then symbols opened in the last RECENT_VIEW_DAYS, then the top
    settings.chain_universe_size symbols by mean option volume of their last 5 stored sessions."""
    now = now or now_ny()
    n = getattr(get_settings(), "chain_universe_size", 150)
    return _unique(user_repo.watchlist_symbols(), views_repo.viewed_since(now - timedelta(days=RECENT_VIEW_DAYS)),
                   metrics_repo.top_by_option_volume(n) if n else [])


def chain_universe_want(now: datetime | None = None) -> dict:
    return {"symbols": chain_universe(now), "max_age_s": CHAIN_MAX_AGE_S,
            "expiries": CHAIN_EXPIRIES, "moneyness": CHAIN_MONEYNESS}


def underlying_stats_want(now: datetime | None = None) -> dict:
    """The universe while today's session runs (open until its eod_deadline), else no symbols."""
    now = (now or now_ny()).astimezone(NY)
    d = now.date()
    live = is_trading_day(d) and datetime.combine(d, OPEN, NY) <= now < eod_deadline(d)
    return {"symbols": symbols_repo.universe_symbols() if live else [], "max_age_s": STATS_MAX_AGE_S}


def eod_chains_want(now: datetime | None = None) -> dict:
    """Closing chains of the current session not yet received from IBKR (chain universe + indices),
    wanted from final_since (close + 20 min) until eod_deadline (20:00 ET)."""
    now = now or now_ny()
    day = session_date(now)
    have = options_repo.eod_sources([day])[day]
    return {"session_date": day,
            "symbols": [s for s in _unique(chain_universe(now), EOD_INDICES) if have.get(s) != options_repo.IBKR],
            "not_before": final_since(day), "deadline": eod_deadline(day)}


def option_bars_want(now: datetime | None = None, record: bool = True) -> list[dict]:
    """[{symbol, sessions, expiries, moneyness}] for chain-universe symbols whose recent sessions
    (catchup.catchup_sessions) have no archive and whose underlying close for them is stored (needed
    for IV). `record`: count this offer (a (session, symbol) is re-offered only REOFFER_AFTER later,
    at most MAX_OFFERS times — a session the bridge has no bars for is not asked for forever)."""
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

    out, offered = [], []
    for s, days in missing.items():
        days = [d for d in days if s in last_bar and d <= last_bar[s] and due(d, s)]
        if days:
            out.append({"symbol": s, "sessions": days, "expiries": catchup.EXPIRIES,
                        "moneyness": catchup.MONEYNESS})
            offered += [(d, s) for d in days]
    if record:
        options_repo.record_catchup_offers(offered, now)
    return out
