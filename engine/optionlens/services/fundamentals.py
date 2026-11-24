"""Key stats of one or many symbols (analytics.fundamentals.key_stats): the one source of P/E, EPS, market cap, shares,
52-week range, average volumes, Questrade's classification and the dividend yield for the symbol header, Summary,
peers and the screeners (universe frame)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Mapping, Sequence

from ..analytics import dividends as divs_a
from ..analytics.fundamentals import key_stats
from ..models import Dividend
from ..repo import dividends as div_repo
from ..repo import fundamentals as fund_repo

DIVIDEND_LOOKBACK_DAYS = 3 * 365   # enough for annual_rate (last 8 payments, one-off window ±2 years)


def indicated(divs: Sequence[Dividend], price: float | None, asof: date) -> tuple[float | None, str | None]:
    """(indicated yield, the source of the latest regular dividend behind it)."""
    y = divs_a.indicated_yield(divs, price, asof)
    reg = [d for d in divs_a.regular(divs) if d.ex_date <= asof]
    return y, (reg[-1].source if reg and y is not None else None)


def for_symbols(prices: Mapping[str, float | None], nasdaq_caps: Mapping[str, float | None], asof: date) -> dict[str, dict]:
    """{symbol: key stats} in bulk, from {symbol: current price (None: unknown)} and the symbols table's market caps
    (the fallback without a snapshot)."""
    syms = list(prices)
    snaps = fund_repo.latest(syms, asof)
    divs = div_repo.by_symbol(syms, asof - timedelta(days=DIVIDEND_LOOKBACK_DAYS))
    return {s: key_stats(snaps.get(s), p, nasdaq_caps.get(s), indicated(divs.get(s, []), p, asof))
            for s, p in prices.items()}


def for_symbol(row: Mapping, price: float | None, asof: date) -> dict:
    """Key stats of one symbols row."""
    return for_symbols({row["symbol"]: price}, {row["symbol"]: row.get("market_cap")}, asof)[row["symbol"]]
