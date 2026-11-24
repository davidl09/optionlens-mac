"""extended_quotes: the latest extended-hours quote per symbol and market ('pre' | 'post') of a session."""
from __future__ import annotations

from datetime import date
from typing import Iterable

from .. import db
from ..models import ExtendedQuote
from ._sql import insert_arrow, query, utc_naive

MARKETS = ("pre", "post")


def upsert(market: str, session: date, quotes: Iterable[ExtendedQuote], source: str) -> int:
    """Replace each symbol's `market` row with its quote of `session`. Returns rows written."""
    if market not in MARKETS:
        raise ValueError(market)
    uniq = list({q.symbol: q for q in quotes}.values())
    with db.write() as cur:
        return insert_arrow(cur, "extended_quotes", {
            "symbol": [q.symbol for q in uniq], "market": [market] * len(uniq), "session_date": [session] * len(uniq),
            "ts": [utc_naive(q.ts) for q in uniq], "price": [q.price for q in uniq],
            "regular_price": [q.regular_price for q in uniq], "volume": [q.volume for q in uniq],
            "last_trade_at": [utc_naive(q.last_trade_at) for q in uniq], "source": [source] * len(uniq),
            "delayed": [q.delayed for q in uniq]}, "INSERT OR REPLACE")


def latest_session(market: str) -> list[dict]:
    """The `market` rows of the newest session collected: {symbol, session_date, ts, price, regular_price, volume,
    last_trade_at, source, delayed}."""
    return query("""SELECT symbol, session_date, ts, price, regular_price, volume, last_trade_at, source, delayed
                    FROM extended_quotes WHERE market = ?
                      AND session_date = (SELECT max(session_date) FROM extended_quotes WHERE market = ?)""",
                 [market, market])
