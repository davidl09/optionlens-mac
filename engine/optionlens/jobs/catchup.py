"""End-of-day archive catch-up: which recent sessions lack an option_eod archive, and reconstruction of
archive rows from historical daily bars of individual contracts (pushed by the IBKR bridge via
/api/ingest/option-bars).

Reconstructed rows carry source='reconstructed': close and volume are real trade data, bid/ask/OI are
NULL, IV and greeks are computed (Black-Scholes) from the contract close and the underlying's daily close,
so they are approximate (the last option trade may precede the underlying's close). They never replace
a real archive, and OI-change comparisons skip them (repo.options.prior_eod_date).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterable, Mapping, Sequence

import numpy as np

from ..analytics import bs, computed_iv
from ..analytics import dividends as div
from ..analytics import markets
from ..analytics.calendar import NY, NYSE, TradingCalendar, now_ny, year_fraction_to_expiry
from ..config import get_settings
from ..repo import bars as bars_repo
from ..repo import dividends as dividends_repo
from ..repo import market as market_repo
from ..repo import options as options_repo
from ..sources.osi import format_osi
from ._util import eod_deadline, session_close

log = logging.getLogger(__name__)

LOOKBACK_SESSIONS = 10
EXPIRIES = 6          # catch-up asks for the first N expiries ...
MONEYNESS = 0.15      # ... with |strike / underlying close − 1| <= this


def catchup_sessions(now: datetime | None = None, n: int = LOOKBACK_SESSIONS, cal: TradingCalendar = NYSE) -> list[date]:
    """The last n trading sessions of `cal` (ascending) whose closing-chain push window (eod_deadline) has ended."""
    now = (now or now_ny()).astimezone(NY)
    d = now.date()
    if not (cal.is_trading_day(d) and eod_deadline(d) <= now):
        d = cal.prev_trading_day(d)
    out: list[date] = []
    while len(out) < n:
        out.append(d)
        d = cal.prev_trading_day(d)
    return sorted(out)


def missing_eod(symbols: Sequence[str], sessions: Sequence[date]) -> dict[str, list[date]]:
    """{symbol: sessions without a real option_eod archive} for symbols missing at least one session. A
    reconstructed archive counts as missing: it is filled contract by contract and may be partial (an
    interrupted catch-up); bridge_plan bounds how often it is offered again."""
    have = options_repo.eod_sources(sessions, symbols)
    out: dict[str, list[date]] = {}
    for d in sessions:
        for s in symbols:
            if have.get(d, {}).get(s, options_repo.RECONSTRUCTED) == options_repo.RECONSTRUCTED:
                out.setdefault(s, []).append(d)
    return out


def dividend_yield(symbol: str, spot: float | None, day: date) -> float:
    """Trailing-12-month dividend yield when stored dividends cover the trailing year, else 0 (no
    dividend data, e.g. non-Nasdaq listings)."""
    divs = dividends_repo.for_symbol(symbol)
    return div.trailing_yield(divs, spot, day) if div.covers_trailing_year(divs, day) else 0.0


def reconstruct_rows(symbol: str, day: date, bars: Iterable[Mapping], spot: float | None, r: float,
                     q: float) -> list[dict]:
    """option_eod rows of `day` from contract bars ({expiry, strike, right, close, volume}): last = close,
    IV by Black-Scholes inversion at the session close (None when no underlying close or the price is
    outside no-arbitrage bounds), greeks from that IV. Skips expired contracts and bars with volume 0."""
    asof = session_close(day, markets.calendar_for(symbol))
    out = []
    for b in bars:
        expiry, strike, right, close = b["expiry"], float(b["strike"]), b["right"], b.get("close")
        if expiry < day or close is None or b.get("volume") == 0:
            continue
        t = year_fraction_to_expiry(expiry, asof)
        iv = bs.implied_vol(float(close), spot, strike, t, r, q, right) if spot else None
        g = bs.greeks(spot, strike, t, r, q, iv, right) if iv else {}
        out.append({
            "osi": format_osi(symbol, expiry, right, strike), "expiry": expiry, "strike": strike,
            "right": right, "last": float(close), "volume": None if b.get("volume") is None else int(b["volume"]),
            "iv": iv, "delta": g.get("delta"), "gamma": g.get("gamma"), "theta": g.get("theta"),
            "vega": g.get("vega"), "underlying": spot,
            "iv_source": computed_iv.COMPUTED_LAST if iv else None, "greeks_source": computed_iv.COMPUTED if iv else None,
        })
    return out


def store_reconstructed_bars(symbol: str, bars: Sequence[Mapping], source: str) -> int:
    """Store pushed daily contract bars ({expiry, strike, right, date, close, volume, open/high/low
    ignored}) of `symbol` as reconstructed option_eod rows, per session date (r: stored 13w bill rate,
    q: dividend_yield). A session that already has a real archive is left alone; in a reconstructed one
    the pushed contracts are replaced and the others kept (the bridge pushes contract by contract).
    `source` is the provider of the bars (logged). The rate is the one of the symbol's currency. Returns rows stored."""
    by_day: dict[date, list[Mapping]] = {}
    for b in bars:
        by_day.setdefault(b["date"], []).append(b)
    if not by_day:
        return 0
    hist = bars_repo.get_bars(symbol, min(by_day), max(by_day))
    closes = {d: float(c) for d, c in zip(hist["date"].astype(object).tolist(), hist["close"].tolist())
              if np.isfinite(c)}
    currency = markets.currency_of(symbol)
    default_r = get_settings().fallback_rate(currency)
    stored = 0
    for day in sorted(by_day):
        spot = closes.get(day)
        r = market_repo.rate_on(day, currency)
        rows = reconstruct_rows(symbol, day, by_day[day], spot, default_r if r is None else r,
                                dividend_yield(symbol, spot, day))
        stored += options_repo.write_reconstructed_eod(day, symbol, rows)
    log.info("reconstructed %s rows of %s from %s bars (%d sessions)", stored, symbol, source, len(by_day))
    return stored
