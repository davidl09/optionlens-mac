"""intraday_bars / intraday_spans: stored intraday bars per (symbol, interval) and the spans already fetched.

Live bars (source LIVE, 1m only, live/bars.py) share intraday_bars without a span: a source's candle of the same
start replaces one, a live bar never replaces a source's candle, and they are kept LIVE_RETENTION."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from .. import db
from ..models import IntradayBar
from ._sql import insert_arrow, query, utc_naive

Span = tuple[datetime, datetime]
LIVE = "live"
LIVE_RETENTION = timedelta(days=3)


def store(symbol: str, interval: str, bars: Sequence[IntradayBar], spans: Sequence[Span], source: str,
          delayed: bool | None, fetched_at: datetime, keep_from: datetime) -> int:
    """Store bars (a bar at the same start is replaced) and the spans they were fetched for, then drop what is older
    than `keep_from` (the interval's retention). Returns bars stored."""
    uniq = list({b.ts: b for b in bars}.values())
    with db.write() as cur:
        n = insert_arrow(cur, "intraday_bars", {
            "symbol": [symbol] * len(uniq), "interval": [interval] * len(uniq), "ts": [utc_naive(b.ts) for b in uniq],
            "open": [b.open for b in uniq], "high": [b.high for b in uniq], "low": [b.low for b in uniq],
            "close": [b.close for b in uniq], "volume": [b.volume for b in uniq], "source": [source] * len(uniq),
            "delayed": [delayed] * len(uniq)}, "INSERT OR REPLACE")
        insert_arrow(cur, "intraday_spans", {
            "symbol": [symbol] * len(spans), "interval": [interval] * len(spans),
            "start_ts": [utc_naive(s) for s, _ in spans], "end_ts": [utc_naive(e) for _, e in spans],
            "source": [source] * len(spans), "fetched_at": [utc_naive(fetched_at)] * len(spans)})
        cut = utc_naive(keep_from)
        cur.execute("DELETE FROM intraday_bars WHERE symbol = ? AND interval = ? AND ts < ?", [symbol, interval, cut])
        cur.execute("DELETE FROM intraday_spans WHERE symbol = ? AND interval = ? AND end_ts <= ?", [symbol, interval, cut])
        return n


def spans(symbol: str, interval: str) -> list[Span]:
    """The fetched spans of (symbol, interval)."""
    rows = query("SELECT start_ts, end_ts FROM intraday_spans WHERE symbol = ? AND interval = ? ORDER BY start_ts",
                 [symbol, interval])
    return [(r["start_ts"], r["end_ts"]) for r in rows]


def bars(symbol: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    """Stored bars starting in [start, end] ascending: {ts (aware UTC), open, high, low, close, volume, source,
    delayed}."""
    return query("""SELECT ts, open, high, low, close, volume, source, delayed FROM intraday_bars
                    WHERE symbol = ? AND interval = ? AND ts >= ? AND ts <= ? ORDER BY ts""",
                 [symbol, interval, utc_naive(start), utc_naive(end)])


def store_live(bars: dict[str, Sequence[IntradayBar]], now: datetime) -> int:
    """Store live 1m bars per symbol (never over a stored bar of the same start), dropping live bars older than
    LIVE_RETENTION. Returns bars stored."""
    rows = [(sym, b) for sym, bs in bars.items() for b in {b.ts: b for b in bs}.values()]
    with db.write() as cur:
        n = insert_arrow(cur, "intraday_bars", {
            "symbol": [s for s, _ in rows], "interval": ["1m"] * len(rows), "ts": [utc_naive(b.ts) for _, b in rows],
            "open": [b.open for _, b in rows], "high": [b.high for _, b in rows], "low": [b.low for _, b in rows],
            "close": [b.close for _, b in rows], "volume": [b.volume for _, b in rows], "source": [LIVE] * len(rows),
            "delayed": [False] * len(rows)}, "INSERT OR IGNORE") if rows else 0
        cur.execute("DELETE FROM intraday_bars WHERE source = ? AND ts < ?", [LIVE, utc_naive(now - LIVE_RETENTION)])
        return n


def live_bars(symbol: str, start: datetime, end: datetime) -> list[dict]:
    """The stored live 1m bars of `symbol` starting in [start, end] (as `bars`)."""
    return query("""SELECT ts, open, high, low, close, volume, source, delayed FROM intraday_bars
                    WHERE symbol = ? AND interval = '1m' AND source = ? AND ts >= ? AND ts <= ? ORDER BY ts""",
                 [symbol, LIVE, utc_naive(start), utc_naive(end)])
