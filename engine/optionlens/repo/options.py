"""option_latest (current chain per symbol) and option_eod (daily end-of-day archive), both tagged with
their `source` and `delayed`. Which real chain becomes a session's archive is decided by the caller
(jobs/precedence.py); here only: a reconstructed archive never replaces a real one."""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Mapping, Sequence

import numpy as np
import pyarrow as pa

from .. import db
from ..models import ChainSnapshot, OptionContract
from ._sql import aware, columns, insert_arrow, query, scalar, split_by, to_columns, utc_naive
from .quotes import get_quote

_CONTRACT_FIELDS = ("symbol", "osi", "expiry", "strike", "right", "bid", "ask", "last", "change",
                    "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "rho", "theo", "last_ts",
                    "iv_source", "greeks_source")
_LATEST_COLS = ('o.symbol, o.osi, o.expiry, o.strike, o."right", o.bid, o.ask, o.last, o.change, '
                'o.volume, o.oi, o.iv, o.delta, o.gamma, o.theta, o.vega, o.rho, o.theo, o.ts, '
                'o.last_ts, o.iv_source, o.greeks_source, q.price AS underlying')
_EOD_COLS = ('date, symbol, osi, expiry, strike, "right", bid, ask, last, volume, oi, iv, delta, '
             'gamma, theta, vega, underlying, iv_source, greeks_source')
_EOD_FIELDS = ("date", "symbol", "osi", "expiry", "strike", "right", "bid", "ask", "last", "volume", "oi",
               "iv", "delta", "gamma", "theta", "vega", "underlying", "iv_source", "greeks_source", "source",
               "delayed", "fetched_at")

IBKR = "ibkr"
RECONSTRUCTED = "reconstructed"


def _syms(symbols: Sequence[str] | None) -> list[str] | None:
    return None if symbols is None else list(symbols)


# ----------------------------------------------------------------------------- option_latest
def replace_option_latest(snap: ChainSnapshot, source: str | None = None, final: bool = False) -> int:
    """Replace all option_latest rows of snap.quote.symbol with snap.contracts (tagged `source`, snap.delayed and
    `final`: a closing snapshot). Which chain is served is the caller's decision (jobs/precedence.py). Returns rows
    written."""
    symbol = snap.quote.symbol
    uniq = list({c.osi: c for c in snap.contracts}.values())
    data = to_columns(uniq, _CONTRACT_FIELDS)
    data["last_ts"] = [utc_naive(t) for t in data["last_ts"]]
    data["ts"] = [utc_naive(snap.fetched_at)] * len(uniq)
    data["source"] = [source] * len(uniq)
    data["delayed"] = [snap.delayed] * len(uniq)
    data["final"] = [final] * len(uniq)
    data["contracts_listed_at"] = [utc_naive(snap.contracts_listed_at) if snap.contracts_listed_at else None] * len(uniq)
    with db.write() as cur:
        cur.execute("DELETE FROM option_latest WHERE symbol = ?", [symbol])
        return insert_arrow(cur, "option_latest", data)


_QUOTED = "ask > 0 AND NOT (COALESCE(bid, 0) = 0 AND COALESCE(oi, 0) = 0 AND COALESCE(volume, 0) = 0)"


def latest_quality(symbol: str) -> dict | None:
    """{source, fetched_at (aware UTC), final, delayed, quoted} of the served chain (jobs/precedence.py), or None.
    `delayed` None when unknown; `quoted`: contracts with an ask > 0 that the archive would keep."""
    rows = query(f"""SELECT any_value(source) AS source, max(ts) AS fetched_at, bool_or(final) AS final,
                            bool_or(delayed) AS delayed, count(*) FILTER (WHERE {_QUOTED}) AS quoted
                     FROM option_latest WHERE symbol = ? HAVING count(*) > 0""", [symbol])
    if not rows:
        return None
    r = rows[0]
    return {**r, "fetched_at": aware(r["fetched_at"]), "final": bool(r["final"]), "quoted": int(r["quoted"])}


def update_source_iv(symbol: str, contracts: Iterable[OptionContract]) -> int:
    """Overwrite IV / greeks and their labels of these served-chain rows (by osi). Returns rows updated."""
    data = to_columns(contracts, ("osi", "iv", "delta", "gamma", "theta", "vega", "iv_source", "greeks_source"))
    if not data["osi"]:
        return 0
    tbl = pa.table(data)
    with db.write() as cur:
        cur.register("_iv_update", tbl)
        try:
            cur.execute("""UPDATE option_latest o SET iv = u.iv, delta = u.delta, gamma = u.gamma, theta = u.theta,
                           vega = u.vega, iv_source = u.iv_source, greeks_source = u.greeks_source
                           FROM _iv_update u WHERE o.symbol = ? AND o.osi = u.osi""", [symbol])
        finally:
            cur.unregister("_iv_update")
    return tbl.num_rows


LIVE_FIELDS = ("bid", "ask", "last", "volume", "oi", "iv", "delta", "gamma", "theta", "vega")


def patch_latest(rows: Sequence[Mapping]) -> int:
    """Live quotes (live/hub.py) into the served chain: each row {osi, source, <LIVE_FIELDS>...} replaces the fields
    it carries (not None) of that served contract; a row with an IV labels iv_source / greeks_source with its source.
    Rows of contracts not in option_latest are skipped; the chain's fetch time, source and `delayed` are kept (they
    describe the chain for jobs/precedence.py). Returns rows given."""
    if not rows:
        return 0
    data: dict[str, list] = {k: [r.get(k) for r in rows] for k in ("osi", "source", *LIVE_FIELDS)}
    data["volume"] = [None if v is None else int(v) for v in data["volume"]]
    data["oi"] = [None if v is None else int(v) for v in data["oi"]]
    tbl = pa.table({k: pa.array(v, type=pa.string() if k in ("osi", "source") else
                                (pa.int64() if k in ("volume", "oi") else pa.float64())) for k, v in data.items()})
    sets = ", ".join(f"{k} = COALESCE(u.{k}, o.{k})" for k in LIVE_FIELDS)
    with db.write() as cur:
        cur.register("_live_patch", tbl)
        try:
            cur.execute(f"""UPDATE option_latest o SET {sets},
                              iv_source = CASE WHEN u.iv IS NOT NULL THEN u.source ELSE o.iv_source END,
                              greeks_source = CASE WHEN u.delta IS NOT NULL THEN u.source ELSE o.greeks_source END
                            FROM _live_patch u WHERE o.osi = u.osi""")
        finally:
            cur.unregister("_live_patch")
    return tbl.num_rows


def symbols_of(osis: Sequence[str]) -> dict[str, str]:
    """{osi: underlying symbol} of the served contracts among `osis`."""
    if not osis:
        return {}
    return {r["osi"]: r["symbol"] for r in query("SELECT osi, symbol FROM option_latest WHERE osi IN (SELECT unnest(?))",
                                                  [list(osis)])}


def osis_in_band(symbol: str, expiry: date, strike_min: float | None, strike_max: float | None) -> list[str]:
    """OSI codes of the served chain's contracts of one expiry within [strike_min, strike_max] (None: unbounded)."""
    return [r["osi"] for r in query(
        """SELECT osi FROM option_latest WHERE symbol = ? AND expiry = ? AND strike >= coalesce(?, -1e18)
           AND strike <= coalesce(?, 1e18) ORDER BY strike, "right" """, [symbol, expiry, strike_min, strike_max])]


def latest_fetched_at(symbol: str) -> datetime | None:
    """Fetch time (aware UTC) of the stored chain, or None if none stored."""
    return aware(scalar("SELECT max(ts) FROM option_latest WHERE symbol = ?", [symbol]))


def latest_source_iv(symbol: str, since: datetime) -> dict[str, dict]:
    """{osi: {iv, delta, gamma, theta, vega, iv_source, greeks_source, source}} of the stored chain's rows with an IV
    its chain source sent (not engine-computed), when that chain was fetched at/after `since`."""
    rows = query("""SELECT osi, iv, delta, gamma, theta, vega, iv_source, greeks_source, source FROM option_latest
                    WHERE symbol = ? AND ts >= ? AND iv > 0 AND iv_source IS NOT NULL
                      AND NOT starts_with(iv_source, 'computed')""", [symbol, utc_naive(since)])
    return {r["osi"]: r for r in rows}


def last_fetched_by(source: str) -> datetime | None:
    """Fetch time (aware UTC) of the newest stored chain from `source`, or None."""
    return aware(scalar("SELECT max(ts) FROM option_latest WHERE source = ?", [source]))


def latest_source(symbol: str) -> str | None:
    """`source` of the stored chain (None when unknown or none stored)."""
    return scalar("SELECT any_value(source) FROM option_latest WHERE symbol = ?", [symbol])


def latest_delayed(symbol: str) -> bool:
    """Whether the stored chain was pushed from delayed data (False when unknown or none stored)."""
    return bool(scalar("SELECT bool_or(delayed) FROM option_latest WHERE symbol = ?", [symbol]))


def latest_contracts_listed_at(symbol: str) -> datetime | None:
    """When the stored chain's contract list was listed, if the client built it from a saved list (IBKR's contract
    server unavailable); None when it was listed at fetch time (or unknown / none stored)."""
    return aware(scalar("SELECT max(contracts_listed_at) FROM option_latest WHERE symbol = ?", [symbol]))


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
    return ChainSnapshot(quote, tuple(OptionContract(**{**r, "last_ts": aware(r["last_ts"])}) for r in rows), fetched)


def expiries(symbol: str) -> list[date]:
    """Distinct stored expiries for a symbol, ascending."""
    return [r["expiry"] for r in query(
        "SELECT DISTINCT expiry FROM option_latest WHERE symbol = ? ORDER BY expiry", [symbol])]


def latest_columns(symbols: Sequence[str] | None = None, right: str | None = None,
                   expiry_min: date | None = None, expiry_max: date | None = None,
                   min_oi: int | None = None, min_volume: int | None = None) -> dict[str, np.ndarray]:
    """Universe-wide option_latest filter -> columns (symbol, osi, expiry, strike, right, bid, ask, last,
    change, volume, oi, iv, delta, gamma, theta, vega, rho, theo, ts, underlying), ordered by symbol, expiry, strike.
    Without min_oi / min_volume, contracts with unknown (NULL) OI or volume are included; with them, only contracts
    whose known value reaches the minimum."""
    return columns(f"""
        SELECT {_LATEST_COLS} FROM option_latest o LEFT JOIN quotes_latest q ON q.symbol = o.symbol
        WHERE (?::VARCHAR[] IS NULL OR o.symbol IN (SELECT unnest(?::VARCHAR[])))
          AND (?::VARCHAR IS NULL OR o."right" = ?)
          AND o.expiry >= COALESCE(?::DATE, DATE '1900-01-01') AND o.expiry <= COALESCE(?::DATE, DATE '2999-12-31')
          AND (?::BIGINT IS NULL OR o.oi >= ?) AND (?::BIGINT IS NULL OR o.volume >= ?)
        ORDER BY o.symbol, o.expiry, o.strike, o."right" """,
        [_syms(symbols), _syms(symbols), right, right, expiry_min, expiry_max, min_oi, min_oi, min_volume, min_volume])


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
def _open_archives(cur, day: date, new: Mapping[str, str | None]) -> list[str]:
    """Symbols of `new` ({symbol: source}) whose archive of `day` may be written: none stored, or one from the
    same source, or a reconstructed one when the new one is real (a reconstruction never replaces a real
    archive). Which of two real sources wins is the caller's decision (jobs/precedence.py)."""
    if not new:
        return []
    have: dict[str, list] = dict(cur.execute(
        """SELECT symbol, list(DISTINCT source) FROM option_eod WHERE date = ?
           AND symbol IN (SELECT unnest(?::VARCHAR[])) GROUP BY symbol""", [day, list(new)]).fetchall())
    return [s for s, src in new.items()
            if all(e == src or (e == RECONSTRUCTED and src != RECONSTRUCTED) for e in have.get(s, []))]


def _clear_eod(cur, day: date, symbols: list[str]) -> None:
    cur.execute("DELETE FROM option_eod WHERE date = ? AND symbol IN (SELECT unnest(?::VARCHAR[]))",
                [day, symbols])


def archivable(c: OptionContract) -> bool:
    """Whether the archive keeps a contract: not (bid, OI and volume all 0 / unknown)."""
    return not ((c.bid or 0) == 0 and (c.oi or 0) == 0 and (c.volume or 0) == 0)


def write_option_eod(day: date, fresh_since: datetime, symbols: Sequence[str] | None = None) -> int:
    """Archive option_latest rows fetched at/after fresh_since as EOD rows for day, with their source and delayed
    flag (skipping rows with bid=0 AND oi=0 AND volume=0), replacing an archive of `day` only when it came from
    the same source or was reconstructed (_open_archives). Returns rows written."""
    syms = _syms(symbols)
    since = utc_naive(fresh_since)
    with db.write() as cur:
        cand = dict(cur.execute(
            """SELECT symbol, any_value(source) FROM option_latest WHERE ts >= ?
               AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[]))) GROUP BY symbol""",
            [since, syms, syms]).fetchall())
        ok = _open_archives(cur, day, cand)
        if not ok:
            return 0
        _clear_eod(cur, day, ok)
        cur.execute(f"""
            INSERT INTO option_eod ({_EOD_COLS}, source, delayed, fetched_at)
            SELECT ?::DATE, o.symbol, o.osi, o.expiry, o.strike, o."right", o.bid, o.ask, o.last, o.volume,
                   o.oi, o.iv, o.delta, o.gamma, o.theta, o.vega, q.price, o.iv_source, o.greeks_source, o.source,
                   o.delayed, o.ts
            FROM option_latest o LEFT JOIN quotes_latest q ON q.symbol = o.symbol
            WHERE o.ts >= ? AND o.symbol IN (SELECT unnest(?::VARCHAR[]))
              AND NOT (COALESCE(o.bid, 0) = 0 AND COALESCE(o.oi, 0) = 0 AND COALESCE(o.volume, 0) = 0)
            ORDER BY o.symbol, o.osi""", [day, since, ok])
        return int(cur.fetchone()[0])


def write_eod_chain(day: date, snap: ChainSnapshot, source: str | None) -> int:
    """Archive a closing chain snapshot as the (day, symbol) EOD rows tagged `source` and snap.delayed, replacing
    the stored archive (same skip rule as write_option_eod; the caller decided precedence). Returns rows written."""
    symbol = snap.quote.symbol
    uniq = [c for c in {c.osi: c for c in snap.contracts}.values() if archivable(c)]
    data = {f: [getattr(c, f) for c in uniq] for f in _EOD_FIELDS[2:16] + ("iv_source", "greeks_source")}
    n = len(uniq)
    data.update(date=[day] * n, symbol=[symbol] * n, underlying=[snap.quote.price] * n, source=[source] * n,
                delayed=[snap.delayed] * n, fetched_at=[utc_naive(snap.fetched_at)] * n)
    with db.write() as cur:
        _clear_eod(cur, day, [symbol])
        return insert_arrow(cur, "option_eod", {f: data[f] for f in _EOD_FIELDS})


def eod_quality(day: date, symbols: Sequence[str] | None = None) -> dict[str, dict]:
    """{symbol: {source, delayed, quoted, fetched_at}} of the real (not reconstructed) archives of `day`: `delayed`
    true when any row is delayed (None when unknown), `quoted` the rows with an ask > 0, `fetched_at` when the
    archived chain was fetched (aware UTC; None for older archives) (jobs/precedence.py)."""
    rows = query("""SELECT symbol, any_value(source) AS source, bool_or(delayed) AS delayed,
                           count(*) FILTER (WHERE ask > 0) AS quoted, max(fetched_at) AS fetched_at
                    FROM option_eod WHERE date = ? AND source IS DISTINCT FROM ?
                      AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
                    GROUP BY symbol""", [day, RECONSTRUCTED, _syms(symbols), _syms(symbols)])
    return {r["symbol"]: {"source": r["source"], "delayed": r["delayed"], "quoted": int(r["quoted"]),
                          "fetched_at": aware(r["fetched_at"])} for r in rows}


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
                source=[RECONSTRUCTED] * n, delayed=[None] * n, fetched_at=[None] * n)
    with db.write() as cur:
        if not n or not _open_archives(cur, day, {symbol: RECONSTRUCTED}):
            return 0
        cur.execute("DELETE FROM option_eod WHERE date = ? AND symbol = ? AND osi IN (SELECT unnest(?::VARCHAR[]))",
                    [day, symbol, data["osi"]])
        return insert_arrow(cur, "option_eod", data)


def eod_sources(days: Sequence[date], symbols: Sequence[str] | None = None) -> dict[date, dict[str, str | None]]:
    """{date: {symbol: source}} of the stored archives on `days` (one source per (date, symbol): the real one
    over a reconstruction)."""
    rows = query("""SELECT date, symbol, list(DISTINCT source) AS src FROM option_eod
                    WHERE date IN (SELECT unnest(?::DATE[]))
                      AND (?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[])))
                    GROUP BY date, symbol""", [list(days), _syms(symbols), _syms(symbols)])
    out: dict[date, dict[str, str | None]] = {d: {} for d in days}
    for r in rows:
        out.setdefault(r["date"], {})[r["symbol"]] = max(r["src"], key=lambda x: x != RECONSTRUCTED)
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
