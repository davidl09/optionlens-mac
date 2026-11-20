"""Universe-wide Nasdaq calendar reports (SPEC §7 "Stocks"): stock splits and IPOs / new listings.
Rows come from the 6 h http_cache (`ondemand.fetch_calendar`); splits are joined to the universe frame."""
from __future__ import annotations

import asyncio
from datetime import date

from ...jobs import ondemand
from . import columns as C
from .base import Col, Output, Param, ReportDef, RunParams, group, today
from .common import frame, p_int, p_select

NOTE_SOURCE = "Nasdaq calendar; may be incomplete. Refreshed every 6 hours; the first load can take a moment."


# ============================================================================ splits
SPLIT_COLS = (C.SYMBOL, Col("name", "Name", "text"), Col("execution_date", "Effective date", "date"),
              Col("ratio", "Ratio", "text", description="Nasdaq's text, new : old shares"),
              Col("split_factor", "Split factor", "num2", description="New shares per old share; < 1 is a reverse split"),
              Col("split_type", "Type", "badge"), Col("in_universe", "In universe", "bool"),
              *group(C.G_QUOTE, (Col("price", "Price", "money"),)), Col("market_cap", "Market Cap", "bigmoney"))
SPLIT_FRAME_KEYS = ("in_universe", "price", "market_cap")


def split_type(factor: float | None) -> str | None:
    return None if factor is None or factor == 1 else "Forward" if factor > 1 else "Reverse"


async def run_splits(p: RunParams) -> Output:
    splits, f = await asyncio.gather(ondemand.fetch_calendar("splits"), asyncio.to_thread(frame))
    pos = f.index()
    rows = []
    for s in splits:
        i = pos.get(s["symbol"])
        known = f.rows([i], SPLIT_FRAME_KEYS)[0] if i is not None else {"in_universe": False, "price": None,
                                                                         "market_cap": None}
        if p["only_universe"] and not known["in_universe"]:
            continue
        rows.append({**s, "split_type": split_type(s["split_factor"]), **known})
    return Output(rows, asof=f.asof)


SPLITS = ReportDef(
    "splits-calendar", "Stock splits", "Stocks", "Announced stock splits: recent (30 days) and upcoming.",
    (Param("only_universe", "Universe only", "bool", False, help="Only symbols in the tracked universe"),),
    SPLIT_COLS, run_splits, ("execution_date", "asc"),
    (NOTE_SOURCE, "Ratio is new : old shares (\"1 : 50\" is a 1-for-50 reverse split, factor 0.02; \"8 : 1\" is "
                  "forward). A bare \"5%\" is a stock dividend (factor 1.05). Nasdaq lists events from 30 days back "
                  "to about three weeks ahead; delay is up to the 6 h cache."))

# ============================================================================ IPOs
IPO_STATUSES = (("priced", "Priced"), ("upcoming", "Upcoming"), ("filed", "Filed"))
IPO_COLS = (C.SYMBOL, Col("company", "Company", "text"), Col("exchange", "Exchange", "text"),
            Col("price_text", "Price", "text", description="Proposed price or range"),
            Col("shares", "Shares", "int"), Col("offer_amount", "Offer amount", "bigmoney"),
            Col("date", "Date", "date", description="Pricing date (priced), expected date (upcoming) or filing date"),
            Col("status", "Status", "badge"))


def _months(start: date, offsets: range) -> list[str]:
    out = []
    for k in offsets:
        n = start.year * 12 + start.month - 1 + k
        out.append(f"{n // 12:04d}-{n % 12 + 1:02d}")
    return out


async def run_ipos(p: RunParams) -> Output:
    status, n = p["status"], p["months"]
    t = today()
    offsets = range(n) if status == "upcoming" else range(1 - n, 1)        # current + next | previous + current
    months = await asyncio.gather(*(ondemand.fetch_calendar("ipos", month=m) for m in _months(t, offsets)))
    rows = [r for month in months for r in month if r["status"] == status]
    rows.sort(key=lambda r: r["date"], reverse=status != "upcoming")
    return Output(rows, presorted=True)


NEW_LISTINGS = ReportDef(
    "new-listings", "IPOs / new listings", "Stocks", "Priced, upcoming and filed IPOs from the Nasdaq calendar.",
    (p_select("status", "Status", "upcoming", IPO_STATUSES),
     p_int("months", "Months", 2, 1, 6, help="Priced/filed: this month and the previous ones; upcoming: this month and the next ones")),
    IPO_COLS, run_ipos, ("date", "asc"),
    (NOTE_SOURCE, "Filed deals often have no ticker yet; upcoming prices may be a range (e.g. 10.00-12.00)."))

REPORTS = (SPLITS, NEW_LISTINGS)
