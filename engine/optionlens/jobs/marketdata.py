"""Market-wide series: VIX family history (CBOE CSVs), the USD 13-week T-bill rate (Treasury) and the CAD 3-month
T-bill rate (Bank of Canada)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from ..analytics.calendar import now_ny
from ..repo import market as market_repo
from ..sources import boc, cboe, treasury
from ._util import log

VIX_FAMILY = ("VIX9D", "VIX", "VIX3M", "VIX6M")
RATE_HISTORY_YEARS = 11
CAD_OVERLAP_DAYS = 10   # re-ask the last days (late revisions)


async def vix() -> str:
    """Full VIX9D/VIX/VIX3M/VIX6M daily history -> index_bars."""
    counts = {}
    for sym in VIX_FAMILY:
        bars = await cboe.fetch_history(sym)
        counts[sym] = await asyncio.to_thread(market_repo.upsert_index_bars, bars)
    return ", ".join(f"{k} {v}" for k, v in counts.items())


async def rates() -> str:
    """13w bill rates for the current year (+ previous year in January; the last 11 years on first run)."""
    now = now_ny()
    first_run = await asyncio.to_thread(market_repo.rate_on) is None
    if first_run:
        years = list(range(now.year - RATE_HISTORY_YEARS + 1, now.year + 1))
    else:
        years = [now.year - 1, now.year] if now.month == 1 else [now.year]
    stored, errors = 0, []
    for y in years:
        try:
            n = await asyncio.to_thread(market_repo.upsert_rates, await treasury.fetch_bill_rates(y))
            stored += n
        except Exception as e:  # noqa: BLE001 - one missing year is not fatal
            log.warning("treasury %s: %s", y, e)
            errors.append(f"{y}: {e}")
    if not stored and errors:
        raise RuntimeError("; ".join(errors))
    latest = await asyncio.to_thread(market_repo.rate_on)
    return f"{stored} rates; latest {latest:.2%}" if latest is not None else f"{stored} rates"


async def rates_cad() -> str:
    """Bank of Canada 3-month T-bill yields since the last stored day (the last RATE_HISTORY_YEARS on first run)."""
    last = await asyncio.to_thread(market_repo.last_rate_date, "CAD")
    today = now_ny().date()
    start = last - timedelta(days=CAD_OVERLAP_DAYS) if last else today - timedelta(days=365 * RATE_HISTORY_YEARS)
    rows = await boc.fetch_tbill_rates(start)
    n = await asyncio.to_thread(market_repo.upsert_rates, rows, "CAD")
    latest = await asyncio.to_thread(market_repo.rate_on, None, "CAD")
    return f"{n} CAD rates; latest {latest:.2%}" if latest is not None else f"{n} CAD rates"
