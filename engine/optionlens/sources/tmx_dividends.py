"""TMX Money dividend histories of Canadian listings (Nasdaq has US listings only). UNOFFICIAL, like sources/tmx.py
(the client and its politeness rules): two of the site's GraphQL fields asked in one request, read 2026-11-26;
tests/fixtures/tmx_dividends_*.json are recorded answers (trimmed).

- getDividendsForSymbol(symbol, page, batch): the quote page's dividend history, newest first, declared future
  ex-dates included: exDate, amount, currency, payableDate, declarationDate, recordDate; hasNextPage when the batch
  did not hold them all. Amounts are as declared (not split-adjusted: CNQ's 1.05 before its 2:1 split, 0.525 after);
  an amount of 0 is a distribution without cash (a fund's reinvested year-end one) or one not yet announced. A
  listing can pay in another currency than it trades in (MG, NTR: USD). No dividends, or an unknown ticker: [].
- getSplitsForSymbol(symbol): [{splitDate, ratio}], ratio = old shares per new share (0.5 for 2:1). A listing
  that never split: an error ("Cannot read properties of undefined") and null.

What is stored: cash amounts in the listing's currency, per ex-date, in today's shares (an amount declared before a
split is multiplied by the ratios of the splits after its ex-date), so they compare with the split-adjusted price
history. Amounts in another currency are left out and counted (they are not converted).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .. import net
from ..analytics import markets
from ..models import Dividend
from . import tmx

BATCH = 500        # dividends per request: a whole history (RY: 158 since 1987) in one
MAX_PAGES = 4
KIND = "cash"

QUERY = """query getDividendsForSymbol($symbol: String!, $page: Int, $batch: Int) {
  dividendHistory: getDividendsForSymbol(symbol: $symbol, page: $page, batch: $batch) {
    pageNumber hasNextPage
    dividends { exDate amount currency payableDate declarationDate recordDate }
  }
  splits: getSplitsForSymbol(symbol: $symbol) { splitDate ratio }
}"""


@dataclass(frozen=True)
class Page:
    rows: list[dict]                       # TMX's dividend rows
    splits: list[tuple[date, float]]       # (split date, old shares per new share)
    more: bool


def _day(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _positive(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def parse_page(payload: Any) -> Page:
    """One answer -> its dividend rows, the listing's splits and whether more pages follow."""
    data = tmx.fields(payload, "getDividendsForSymbol")
    history = data.get("dividendHistory")
    if not isinstance(history, dict) or not isinstance(history.get("dividends"), list):
        raise tmx.Unexpected("getDividendsForSymbol: no dividend list")
    splits = [(d, r) for s in data.get("splits") or [] if isinstance(s, dict)
              and (d := _day(s.get("splitDate"))) and (r := _positive(s.get("ratio")))]
    return Page([r for r in history["dividends"] if isinstance(r, dict)], splits, bool(history.get("hasNextPage")))


def dividends(symbol: str, rows: list[dict], splits: list[tuple[date, float]]) -> tuple[list[Dividend], dict[str, int]]:
    """TMX's rows -> (the symbol's cash dividends in its currency, one per ex-date, in today's shares, ascending;
    {other currency: rows left out}). Rows without an ex-date or a positive amount are not dividends to show."""
    currency = markets.currency_of(symbol)
    by_ex: dict[date, Dividend] = {}
    other: dict[str, int] = {}
    for r in rows:
        ex, amount = _day(r.get("exDate")), _positive(r.get("amount"))
        if ex is None or amount is None:
            continue
        paid_in = str(r.get("currency") or "").upper()
        if paid_in != currency:
            if paid_in:                    # (a row without a currency, e.g. one of ENB's oldest: left out, not counted)
                other[paid_in] = other.get(paid_in, 0) + 1
            continue
        for split_day, ratio in splits:
            if ex < split_day:
                amount *= ratio
        amount = round(amount, 6)
        if ex in by_ex:                    # two payments with one ex-date (a regular and a special one): their sum
            amount = round(amount + by_ex[ex].amount, 6)
        by_ex[ex] = Dividend(symbol, ex, amount, _day(r.get("payableDate")), _day(r.get("recordDate")),
                             _day(r.get("declarationDate")), KIND, source=tmx.NAME)
    return [by_ex[d] for d in sorted(by_ex)], other


async def fetch_history(symbol: str) -> tuple[list[Dividend], dict[str, int]]:
    """The dividend history of a Canadian listing (see `dividends`); FetchError when TMX fails or answers
    unexpectedly."""
    ticker = tmx.ticker(symbol)
    rows: list[dict] = []
    splits: list[tuple[date, float]] = []
    for page in range(1, MAX_PAGES + 1):
        payload = await tmx.query("getDividendsForSymbol", QUERY, {"symbol": ticker, "page": page, "batch": BATCH})
        try:
            got = parse_page(payload)
        except tmx.Unexpected as e:
            raise net.FetchError(tmx.URL, 200, f"TMX {e}") from e
        rows += got.rows
        splits = got.splits
        if not got.more:
            break
    return dividends(symbol, rows, splits)
