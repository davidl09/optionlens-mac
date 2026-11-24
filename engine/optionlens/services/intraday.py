"""Intraday bars: GET /api/symbol/{sym}/bars?interval=… (docs/INTEGRATION.md §5 "Intraday bars").

Bars are stored per (symbol, interval) with the spans already fetched (repo.intraday); a request fetches only the
missing spans, from the server-side candle source (Questrade) when it is available. Spans no source here could fill
are answered as `missing`: the desktop app then asks the IBKR bridge, which pushes them to
POST /api/ingest/intraday-bars (`store_pushed`). Concurrent requests for the same series share one fetch.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
from typing import Protocol

from ..analytics import intraday
from ..models import IntradayBar, normalize_symbol
from ..net import FetchError
from ..repo import intraday as repo
from ..sources import chain_source
from ..sources.chain_source import NotListed
from . import context as ctx
from .errors import BadRequest

Span = intraday.Span
MAX_PUSH_BARS = 20_000
MAX_PUSH_SPANS = 100


class CandleSource(Protocol):
    name: str

    def unavailable(self) -> str | None: ...

    async def fetch_candles(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[IntradayBar]: ...


_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _candle_sources() -> list[CandleSource]:
    """The configured server-side chain sources that also serve candles (Questrade)."""
    return [s for s in chain_source.server_sources() if hasattr(s, "fetch_candles")]


def _store(symbol: str, interval: str, bars: list[IntradayBar], spans: list[Span], source: str,
           delayed: bool | None, now: datetime) -> int:
    """Stores bars and the parts of `spans` that are settled (older than intraday.RECENT)."""
    settled = [(s, intraday.settled(e, now)) for s, e in spans]
    return repo.store(symbol, interval, bars, [(s, e) for s, e in settled if e > s], source, delayed, now,
                      now - intraday.RETENTION[interval])


def _gaps(symbol: str, interval: str, want: Span) -> list[Span]:
    """Spans of `want` not fetched yet that hold session hours."""
    return [g for g in intraday.missing(repo.spans(symbol, interval), want) if intraday.session_windows(g)]


async def _fill(symbol: str, interval: str, gaps: list[Span], now: datetime) -> tuple[list[Span], str | None]:
    """Fetches `gaps` from the first available candle source. -> (spans left unfilled, why)."""
    sources = _candle_sources()
    if not sources:
        return gaps, None
    usable = [s for s in sources if s.unavailable() is None]
    if not usable:
        s = sources[0]
        return gaps, f"{s.name}: {s.unavailable()}"
    src = usable[0]
    for i, (a, b) in enumerate(gaps):
        try:
            bars = await src.fetch_candles(symbol, interval, a, b)
        except NotListed:
            return gaps[i:], f"{src.name} does not list {symbol}"
        except FetchError as e:
            return gaps[i:], f"{src.name}: {getattr(e, 'reason', None) or e}"
        await asyncio.to_thread(_store, symbol, interval, bars, [(a, b)], src.name,
                                getattr(src, "last_delayed", None), now)
    return [], None


def _summary(rows: list[dict]) -> tuple[list[str], bool | None]:
    """Sources of the bars (most bars first) and whether they are delayed (null: unknown or mixed)."""
    sources = [s for s, _ in Counter(r["source"] for r in rows).most_common()]
    flags = {r["delayed"] for r in rows}
    return sources, flags.pop() if len(flags) == 1 else None


async def bars(symbol: str, interval: str, rng: str, extended: bool, now: datetime | None = None) -> dict:
    """IntradayBars (types.ts) of `symbol`: bars of `rng` at `interval`, regular hours unless `extended`."""
    try:
        intraday.check(interval, rng)
    except ValueError as e:
        raise BadRequest(str(e)) from e
    sym = (await asyncio.to_thread(ctx.ensure_known, symbol))["symbol"]
    now = now or datetime.now(timezone.utc)
    want = intraday.window(rng, now, extended)
    lock = _locks.setdefault((sym, interval), asyncio.Lock())
    async with lock:
        gaps = await asyncio.to_thread(_gaps, sym, interval, want)
        missing, problem = await _fill(sym, interval, gaps, now) if gaps else ([], None)
    rows = await asyncio.to_thread(repo.bars, sym, interval, *want)
    has_extended = any(not intraday.is_regular(r["ts"]) for r in rows)
    shown = rows if extended else [r for r in rows if intraday.is_regular(r["ts"])]
    sources, delayed = _summary(shown)
    return {
        "symbol": sym, "interval": interval, "range": rng, "extended": extended,
        "start": want[0], "end": want[1], "has_extended": has_extended,
        "bars": [{k: r[k] for k in ("ts", "open", "high", "low", "close", "volume")} for r in shown],
        "sources": sources, "delayed": delayed,
        "missing": [{"start": a, "end": b} for a, b in missing], "problem": problem,
    }


def store_pushed(body: dict, now: datetime | None = None) -> dict:
    """POST /api/ingest/intraday-bars: bars a client fetched for `spans` (the bridge, from IBKR)."""
    interval = body["interval"]
    if interval not in intraday.INTERVAL_S:
        raise BadRequest(f"interval must be one of {', '.join(intraday.INTERVAL_S)}")
    sym = normalize_symbol(body["symbol"])
    spans = [(s["start"], s["end"]) for s in body["spans"]]
    if any(e <= s for s, e in spans):
        raise BadRequest("every span needs start < end")
    bars = [IntradayBar(b["ts"], b["open"], b["high"], b["low"], b["close"],
                        int(b["volume"]) if b.get("volume") is not None else None) for b in body["bars"]]
    n = _store(sym, interval, bars, spans, body["source"], body.get("delayed"), now or datetime.now(timezone.utc))
    return {"stored": n, "symbol": sym}
