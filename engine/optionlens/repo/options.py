"""option_latest (current chain per symbol) and option_eod (daily end-of-day archive), both tagged with
their `source`. Archive precedence per (date, symbol): see `outranks`."""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Mapping, Sequence

import numpy as np

from .. import db
from ..models import ChainSnapshot, OptionContract
from ._sql import aware, columns, insert_arrow, query, scalar, split_by, to_columns, utc_naive
from .quotes import get_quote

_CONTRACT_FIELDS = ("symbol", "osi", "expiry", "strike", "right", "bid", "ask", "last", "change",
                    "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "rho", "theo")
_LATEST_COLS = ('o.symbol, o.osi, o.expiry, o.strike, o."right", o.bid, o.ask, o.last, o.change, '
                'o.volume, o.oi, o.iv, o.delta, o.gamma, o.theta, o.vega, o.rho, o.theo, o.ts, '
                'q.price AS underlying')
_EOD_COLS = ('date, symbol, osi, expiry, strike, "right", bid, ask, last, volume, oi, iv, delta, '
             'gamma, theta, vega, underlying')
_EOD_FIELDS = ("date", "symbol", "osi", "expiry", "strike", "right", "bid", "ask", "last", "volume", "oi",
               "iv", "delta", "gamma", "theta", "vega", "underlying", "source")

IBKR = "ibkr"
RECONSTRUCTED = "reconstructed"


def _rank(source: str | None) -> int:
    return 2 if source == IBKR else 0 if source == RECONSTRUCTED else 1


def outranks(existing: str | None, new: str | None) -> bool:
    """Whether session data from `existing` must not be replaced by data from `new` for the same
    session: IBKR (real OPRA quotes) beats every other source; any real chain beats a reconstruction."""
    return _rank(existing) > _rank(new)


def _syms(symbols: Sequence[str] | None) -> list[str] | None:
    return None if symbols is None else list(symbols)


# ----------------------------------------------------------------------------- option_latest
def replace_option_latest(snap: ChainSnapshot, source: str | None = None) -> int:
    """Replace all option_latest rows of snap.quote.symbol with snap.contracts (tagged `source` and
    snap.delayed). Returns rows written."""
    symbol = snap.quote.symbol
    uniq = list({c.osi: c for c in snap.contracts}.values())
    data = to_columns(uniq, _CONTRACT_FIELDS)
    data["ts"] = [utc_naive(snap.fetched_at)] * len(uniq)
    data["source"] = [source] * len(uniq)
    data["delayed"] = [snap.delayed] * len(uniq)
    with db.write() as cur:
        cur.execute("DELETE FROM option_latest WHERE symbol = ?", [symbol])
        return insert_arrow(cur, "option_latest", data)


def latest_fetched_at(symbol: str) -> datetime | None:
    """Fetch time (aware UTC) of the stored chain, or None if none stored."""
    return aware(scalar("SELECT max(ts) FROM option_latest WHERE symbol = ?", [symbol]))


def latest_source(symbol: str) -> str | None:
    """`source` of the stored chain (None when unknown or none stored)."""
    return scalar("SELECT any_value(source) FROM option_latest WHERE symbol = ?", [symbol])


def latest_delayed(symbol: str) -> bool:
    """Whether the stored chain was pushed from delayed data (False when unknown or none stored)."""
    return bool(scalar("SELECT bool_or(delayed) FROM option_latest WHERE symbol = ?", [symbol]))


def fetched_since(since: datetime, symbols: Sequence[str] | None = None) -> set[str]:
    """Symbols (all, or among `symbols`) whose stored chain was fetched at/after `since`."""
    syms = _syms(symbols)
    return {r["symbol"] for r in query(
        """SELECT DISTINCT symbol FROM option_latest WHERE ts >= ?
           AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))""",
        [utc_naive(since), syms, syms])}


def chain_coverage(since: date, symbols: Sequence[str]) -> int:
    """Number of `symbols` with a chain stored since `since`: a latest chain fetched on/after it or an end-of-day
    archive dated on/after it."""
    syms = list(symbols)
    return int(scalar(
        """SELECT count(*) FROM (SELECT symbol FROM option_latest WHERE ts >= ? UNION
                                 SELECT symbol FROM option_eod WHERE date >= ?)
           WHERE symbol IN (SELECT unnest(?::VARCHAR[]))""",
        [utc_naive(datetime.combine(since, datetime.min.time())), since, syms]) or 0)


def get_chain(symbol: str, expiry: date | None = None) -> ChainSnapshot | None:
    """Stored chain (optionally one expiry) + stored quote as a ChainSnapshot, or None."""
    quote = get_quote(symbol)
    rows = query(f"""SELECT {", ".join(f'"{c}"' for c in _CONTRACT_FIELDS)}, ts FROM option_latest
                     WHERE symbol = ? AND (?::DATE IS NULL OR expiry = ?) ORDER BY expiry, strike, "right" """,
                 [symbol, expiry, expiry])
    if quote is None or not rows:
        return None
    fetched = max(r.pop("ts") for r in rows)
    return ChainSnapshot(quote, tuple(OptionContract(**r) for r in rows), fetched)


def expiries(symbol: str) -> list[date]:
    """Distinct stored expiries for a symbol, ascending."""
    return [r["expiry"] for r in query(
        "SELECT DISTINCT expiry FROM option_latest WHERE symbol = ? ORDER BY expiry", [symbol])]


def latest_columns(symbols: Sequence[str] | None = None, right: str | None = None,
                   expiry_min: date | None = None, expiry_max: date | None = None,
                   min_oi: int | None = None, min_volume: int | None = None) -> dict[str, np.ndarray]:
    """Universe-wide option_latest filter -> columns (symbol, osi, expiry, strike, right, bid, ask, last,
    change, volume, oi, iv, delta, gamma, theta, vega, rho, theo, ts, underlying), ordered by symbol, expiry, strike."""
    return columns(f"""
        SELECT {_LATEST_COLS} FROM option_latest o LEFT JOIN quotes_latest q ON q.symbol = o.symbol
        WHERE (?::VARCHAR[] IS NULL OR o.symbol IN (SELECT unnest(?::VARCHAR[])))
          AND (?::VARCHAR IS NULL OR o."right" = ?)
          AND o.expiry >= COALESCE(?::DATE, DATE '1900-01-01') AND o.expiry <= COALESCE(?::DATE, DATE '2999-12-31')
          AND o.oi >= COALESCE(?::BIGINT, 0) AND o.volume >= COALESCE(?::BIGINT, 0)
        ORDER BY o.symbol, o.expiry, o.strike, o."right" """,
        [_syms(symbols), _syms(symbols), right, right, expiry_min, expiry_max, min_oi, min_volume])


def latest_by_symbol(symbols: Sequence[str] | None = None, right: str | None = None,
                     expiry_min: date | None = None, expiry_max: date | None = None,
                     min_oi: int | None = None, min_volume: int | None = None) -> dict[str, dict[str, np.ndarray]]:
    """latest_columns (one query) split per symbol -> {symbol: columns without 'symbol'}."""
    return split_by(latest_columns(symbols, right, expiry_min, expiry_max, min_oi, min_volume), "symbol")


def near_atm_by_symbol(expiry_min: date, expiry_max: date, band: float,
                       symbols: Sequence[str] | None = None) -> dict[str, dict[str, np.ndarray]]:
    """option_latest rows with expiry in [expiry_min, expiry_max] and |strike/underlying − 1| <= band
    (one query) -> {symbol: {osi, expiry, strike, right, bid, ask, last, ts, underlying}}."""
    return split_by(columns("""
        SELECT o.symbol, o.osi, o.expiry, o.strike, o."right", o.bid, o.ask, o.last, o.ts, q.price AS underlying
        FROM option_latest o JOIN quotes_latest q ON q.symbol = o.symbol
        WHERE q.price > 0 AND abs(o.strike / q.price - 1) <= ? AND o.expiry BETWEEN ? AND ?
          AND (?::VARCHAR[] IS NULL OR o.symbol IN (SELECT unnest(?::VARCHAR[])))
        ORDER BY o.symbol, o.expiry, o.strike, o."right" """,
        [band, expiry_min, expiry_max, _syms(symbols), _syms(symbols)]), "symbol")


# ----------------------------------------------------------------------------- option_eod
def _archivable(cur, day: date, new: Mapping[str, str | None]) -> list[str]:
    """Symbols of `new` ({symbol: source}) whose archive of `day` may be (re)written: no stored rows,
    or none from a source that outranks the new one."""
    if not new:
        return []
    have: dict[str, list] = dict(cur.execute(
        """SELECT symbol, list(DISTINCT source) FROM option_eod WHERE date = ?
           AND symbol IN (SELECT unnest(?::VARCHAR[])) GROUP BY symbol""", [day, list(new)]).fetchall())
    return [s for s, src in new.items() if not any(outranks(e, src) for e in have.get(s, []))]


def _clear_eod(cur, day: date, symbols: list[str]) -> None:
    cur.execute("DELETE FROM option_eod WHERE date = ? AND symbol IN (SELECT unnest(?::VARCHAR[]))",
                [day, symbols])


def _keep(c: OptionContract) -> bool:
    return not ((c.bid or 0) == 0 and (c.oi or 0) == 0 and (c.volume or 0) == 0)


def write_option_eod(day: date, fresh_since: datetime, symbols: Sequence[str] | None = None) -> int:
    """Archive option_latest rows fetched at/after fresh_since as EOD rows for day, with their source
    (skipping rows with bid=0 AND oi=0 AND volume=0). Replaces the (day, symbol) archive unless it came
    from a source that outranks the stored chain's (see `outranks`). Returns rows written."""
    syms = _syms(symbols)
    since = utc_naive(fresh_since)
    with db.write() as cur:
        cand = dict(cur.execute(
            """SELECT symbol, any_value(source) FROM option_latest WHERE ts >= ?
               AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[]))) GROUP BY symbol""",
            [since, syms, syms]).fetchall())
        ok = _archivable(cur, day, cand)
        if not ok:
            return 0
        _clear_eod(cur, day, ok)
        cur.execute(f"""
            INSERT INTO option_eod ({_EOD_COLS}, source)
            SELECT ?::DATE, o.symbol, o.osi, o.expiry, o.strike, o."right", o.bid, o.ask, o.last, o.volume,
                   o.oi, o.iv, o.delta, o.gamma, o.theta, o.vega, q.price, o.source
            FROM option_latest o LEFT JOIN quotes_latest q ON q.symbol = o.symbol
            WHERE o.ts >= ? AND o.symbol IN (SELECT unnest(?::VARCHAR[]))
              AND NOT (COALESCE(o.bid, 0) = 0 AND COALESCE(o.oi, 0) = 0 AND COALESCE(o.volume, 0) = 0)
            ORDER BY o.symbol, o.osi""", [day, since, ok])
        return int(cur.fetchone()[0])


def write_eod_chain(day: date, snap: ChainSnapshot, source: str | None) -> int:
    """Archive a closing chain snapshot as the (day, symbol) EOD rows tagged `source` (same skip and
    precedence rules as write_option_eod). Returns rows written (0 when outranked)."""
    symbol = snap.quote.symbol
    uniq = [c for c in {c.osi: c for c in snap.contracts}.values() if _keep(c)]
    data = {f: [getattr(c, f) for c in uniq] for f in _EOD_FIELDS[2:16]}
    n = len(uniq)
    data.update(date=[day] * n, symbol=[symbol] * n, underlying=[snap.quote.price] * n, source=[source] * n)
    with db.write() as cur:
        if not _archivable(cur, day, {symbol: source}):
            return 0
        _clear_eod(cur, day, [symbol])
        return insert_arrow(cur, "option_eod", {f: data[f] for f in _EOD_FIELDS})


def write_reconstructed_eod(day: date, symbol: str, rows: Iterable[Mapping]) -> int:
    """Add reconstructed EOD rows (dicts with osi, expiry, strike, right, last, volume, iv, delta,
    gamma, theta, vega, underlying; bid/ask/oi NULL) to the (day, symbol) archive with source
    'reconstructed' — only when that archive is missing or itself reconstructed (real rows are never
    overwritten). Rows with the same osi are replaced, the archive's other contracts are kept. Returns
    rows written (0 when a real archive exists)."""
    rows = list({r["osi"]: r for r in rows}.values())
    n = len(rows)
    data = {f: [r.get(f) for r in rows] for f in _EOD_FIELDS}
    data.update(date=[day] * n, symbol=[symbol] * n, bid=[None] * n, ask=[None] * n, oi=[None] * n,
                source=[RECONSTRUCTED] * n)
    with db.write() as cur:
        if not n or not _archivable(cur, day, {symbol: RECONSTRUCTED}):
            return 0
        cur.execute("DELETE FROM option_eod WHERE date = ? AND symbol = ? AND osi IN (SELECT unnest(?::VARCHAR[]))",
                    [day, symbol, data["osi"]])
        return insert_arrow(cur, "option_eod", data)


def eod_sources(days: Sequence[date], symbols: Sequence[str] | None = None) -> dict[date, dict[str, str | None]]:
    """{date: {symbol: source}} of the stored archives on `days` (one source per (date, symbol):
    the highest-ranking one)."""
    rows = query("""SELECT date, symbol, list(DISTINCT source) AS src FROM option_eod
                    WHERE date IN (SELECT unnest(?::DATE[]))
                      AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
                    GROUP BY date, symbol""", [list(days), _syms(symbols), _syms(symbols)])
    out: dict[date, dict[str, str | None]] = {d: {} for d in days}
    for r in rows:
        out.setdefault(r["date"], {})[r["symbol"]] = max(r["src"], key=_rank)
    return out


def catchup_offers(days: Sequence[date]) -> dict[tuple[date, str], tuple[datetime, int]]:
    """{(date, symbol): (last offered_at aware UTC, offers)} of option-bars catch-up offers on `days`."""
    return {(r["date"], r["symbol"]): (r["offered_at"], r["offers"]) for r in query(
        "SELECT date, symbol, offered_at, offers FROM eod_catchup WHERE date IN (SELECT unnest(?::DATE[]))",
        [list(days)])}


def record_catchup_offers(pairs: Iterable[tuple[date, str]], at: datetime) -> None:
    """Count one more offer of each (date, symbol) at `at`."""
    pairs = list(dict.fromkeys(pairs))
    if not pairs:
        return
    with db.write() as cur:
        cur.executemany("""INSERT INTO eod_catchup VALUES (?, ?, ?, 1) ON CONFLICT (date, symbol)
                           DO UPDATE SET offered_at = excluded.offered_at, offers = eod_catchup.offers + 1""",
                        [[d, s, utc_naive(at)] for d, s in pairs])


def eod_for_date(day: date, symbols: Sequence[str] | None = None, right: str | None = None,
                 expiry_min: date | None = None, expiry_max: date | None = None) -> dict[str, np.ndarray]:
    """option_eod rows of one date (filtered) -> columns (date, symbol, osi, expiry, strike, right, bid, ask,
    last, volume, oi, iv, delta, gamma, theta, vega, underlying) ordered by symbol, osi."""
    return columns(f"""
        SELECT {_EOD_COLS} FROM option_eod WHERE date = ?
          AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
          AND (?::VARCHAR IS NULL OR "right" = ?)
          AND expiry >= COALESCE(?::DATE, DATE '1900-01-01') AND expiry <= COALESCE(?::DATE, DATE '2999-12-31')
        ORDER BY symbol, osi""", [day, _syms(symbols), _syms(symbols), right, right, expiry_min, expiry_max])


def eod_for_symbol(symbol: str, start: date | None = None, end: date | None = None,
                   expiry: date | None = None) -> dict[str, np.ndarray]:
    """option_eod rows of a symbol over [start, end] (optionally one expiry) -> columns ordered by date, osi."""
    return columns(f"""
        SELECT {_EOD_COLS} FROM option_eod WHERE symbol = ?
          AND date >= COALESCE(?::DATE, DATE '1900-01-01') AND date <= COALESCE(?::DATE, DATE '2999-12-31')
          AND (?::DATE IS NULL OR expiry = ?)
        ORDER BY date, osi""", [symbol, start, end, expiry, expiry])


def eod_for_osi(osi: str) -> dict[str, np.ndarray]:
    """option_eod history of one contract -> columns ordered by date."""
    return columns(f"SELECT {_EOD_COLS} FROM option_eod WHERE osi = ? ORDER BY date", [osi])


def prior_eod_date(before: date, symbol: str | None = None) -> date | None:
    """Latest option_eod date strictly before `before` holding real archives (with open interest; a
    reconstructed archive has none) — that symbol's own latest when given — or None."""
    return scalar(f"""SELECT max(date) FROM option_eod WHERE date < ? AND source IS DISTINCT FROM '{RECONSTRUCTED}'
                      AND (?::VARCHAR IS NULL OR symbol = ?)""", [before, symbol, symbol])


def prior_oi(before: date, symbol: str | None = None) -> dict[str, int]:
    """{osi: oi} from the latest real (not reconstructed) option_eod date < before — the symbol's own latest EOD date when
    symbol is given (all symbols' latest common date otherwise); {} when there is none."""
    d = prior_eod_date(before, symbol)
    if d is None:
        return {}
    rows = db.cursor().execute(
        f"""SELECT osi, oi FROM option_eod WHERE date = ? AND source IS DISTINCT FROM '{RECONSTRUCTED}'
            AND (?::VARCHAR IS NULL OR symbol = ?)""",
        [d, symbol, symbol]).fetchall()
    return {osi: int(oi or 0) for osi, oi in rows}


def eod_dates(symbol: str | None = None) -> list[date]:
    """Distinct option_eod dates (optionally for one symbol), ascending."""
    return [r["date"] for r in query(
        "SELECT DISTINCT date FROM option_eod WHERE (?::VARCHAR IS NULL OR symbol = ?) ORDER BY date",
        [symbol, symbol])]


def prune_option_eod(before: date) -> int:
    """Delete option_eod rows dated before `before`. Returns rows deleted."""
    with db.write() as cur:
        cur.execute("DELETE FROM option_eod WHERE date < ?", [before])
        return int(cur.fetchone()[0])


def latest_vs_eod_oi(prior_date: date, symbols: Sequence[str] | None = None, min_abs_change: int = 0,
                     expiry_min: date | None = None, expiry_max: date | None = None,
                     limit: int | None = None) -> dict[str, np.ndarray]:
    """option_latest contracts joined to their option_eod row of prior_date (contracts absent that day
    are skipped) with |oi − oi_prev| >= min_abs_change -> columns (symbol, osi, expiry, strike, right, bid,
    ask, last, volume, oi, oi_prev, iv, delta, underlying), ordered by |change| desc (top `limit`)."""
    return columns("""
        SELECT o.symbol, o.osi, o.expiry, o.strike, o."right", o.bid, o.ask, o.last, o.volume, o.oi,
               e.oi AS oi_prev, o.iv, o.delta, q.price AS underlying
        FROM option_latest o JOIN option_eod e ON e.date = ? AND e.osi = o.osi
             AND e.source IS DISTINCT FROM 'reconstructed'
        LEFT JOIN quotes_latest q ON q.symbol = o.symbol
        WHERE abs(COALESCE(o.oi, 0) - COALESCE(e.oi, 0)) >= ?
          AND (?::VARCHAR[] IS NULL OR o.symbol IN (SELECT unnest(?::VARCHAR[])))
          AND o.expiry >= COALESCE(?::DATE, DATE '1900-01-01') AND o.expiry <= COALESCE(?::DATE, DATE '2999-12-31')
        ORDER BY abs(COALESCE(o.oi, 0) - COALESCE(e.oi, 0)) DESC, o.osi
        LIMIT COALESCE(?::BIGINT, 9223372036854775807)""",
        [prior_date, min_abs_change, _syms(symbols), _syms(symbols), expiry_min, expiry_max, limit])
