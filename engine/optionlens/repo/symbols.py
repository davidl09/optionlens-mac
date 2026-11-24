"""symbols table: universe membership, kind, sector/industry, optionable flag (a symbol's market: analytics/markets.py)."""
from __future__ import annotations

from typing import Iterable

import pyarrow as pa

from .. import db
from ..analytics import markets
from ._sql import now_utc, one, query, scalar

_FIELDS = ("symbol", "name", "kind", "sector", "industry", "market_cap", "dividend_yield")


def upsert_symbols(rows: Iterable[dict]) -> int:
    """Insert/update descriptive fields {symbol, kind, name?, sector?, industry?, market_cap?, dividend_yield?}. NULL inputs never overwrite stored values; in_universe/optionable are untouched. Returns count."""
    rows = list({r["symbol"]: r for r in rows}.values())
    if not rows:
        return 0
    data = {f: [r.get(f) for r in rows] for f in _FIELDS}
    data["updated_at"] = [now_utc()] * len(rows)
    schema = pa.schema([(f, pa.float64() if f in ("market_cap", "dividend_yield") else pa.string()) for f in _FIELDS]
                       + [("updated_at", pa.timestamp("us"))])
    with db.write() as cur:
        cur.register("_sym_in", pa.table(data, schema=schema))
        try:
            cur.execute("""
                INSERT INTO symbols (symbol, name, kind, sector, industry, market_cap, dividend_yield, updated_at)
                SELECT symbol, name, kind, sector, industry, market_cap, dividend_yield, updated_at FROM _sym_in
                ON CONFLICT (symbol) DO UPDATE SET
                  name = COALESCE(excluded.name, symbols.name),
                  kind = excluded.kind,
                  sector = COALESCE(excluded.sector, symbols.sector),
                  industry = COALESCE(excluded.industry, symbols.industry),
                  market_cap = COALESCE(excluded.market_cap, symbols.market_cap),
                  dividend_yield = COALESCE(excluded.dividend_yield, symbols.dividend_yield),
                  updated_at = excluded.updated_at""")
        finally:
            cur.unregister("_sym_in")
    return len(rows)


def set_universe(symbols: Iterable[str], market: markets.Market = markets.US) -> int:
    """Mark exactly these (existing) symbols of `market` in_universe; the market's other symbols false (the other
    market's universe is untouched). Returns the market's universe size."""
    syms = sorted(s for s in set(symbols) if markets.market_of(s) is market)
    with db.write() as cur:
        current = [r[0] for r in cur.execute("SELECT symbol FROM symbols WHERE in_universe").fetchall()]
        drop = [s for s in current if markets.market_of(s) is market]
        cur.execute("UPDATE symbols SET in_universe = false WHERE symbol IN (SELECT unnest(?::VARCHAR[]))", [drop])
        cur.execute("UPDATE symbols SET in_universe = true WHERE symbol IN (SELECT unnest(?::VARCHAR[]))", [syms])
        now = [r[0] for r in cur.execute("SELECT symbol FROM symbols WHERE in_universe").fetchall()]
    return sum(1 for s in now if markets.market_of(s) is market)


def set_optionable(symbol: str, optionable: bool) -> None:
    """Record whether CBOE lists options for the symbol."""
    with db.write() as cur:
        cur.execute("UPDATE symbols SET optionable = ? WHERE symbol = ? AND optionable IS DISTINCT FROM ?",
                    [optionable, symbol, optionable])


def reprobe_not_optionable(symbols: Iterable[str]) -> None:
    """Reset optionable=false to NULL (unknown) for these symbols so the next sweep probes them again."""
    with db.write() as cur:
        cur.execute("UPDATE symbols SET optionable = NULL WHERE optionable = false "
                    "AND symbol IN (SELECT unnest(?::VARCHAR[]))", [sorted(set(symbols))])


def get_symbol(symbol: str) -> dict | None:
    """-> {symbol, name, kind, sector, industry, market_cap, in_universe, optionable, updated_at} or None."""
    cur = db.cursor()
    cur.execute("SELECT * FROM symbols WHERE symbol = ?", [symbol])
    return one(cur)


def list_symbols(*, in_universe: bool | None = None, kind: str | None = None,
                 optionable: bool | None = None) -> list[dict]:
    """Symbols rows filtered by flags (None = any), ordered by market cap desc then symbol."""
    where, params = [], []
    if in_universe is not None:
        where.append("in_universe = ?"); params.append(in_universe)
    if kind is not None:
        where.append("kind = ?"); params.append(kind)
    if optionable is not None:
        where.append("optionable IS NOT DISTINCT FROM ?"); params.append(optionable)
    sql = "SELECT * FROM symbols" + (" WHERE " + " AND ".join(where) if where else "")
    return query(sql + " ORDER BY market_cap DESC NULLS LAST, symbol", params)


def universe_symbols(include_unoptionable: bool = False) -> list[str]:
    """Universe tickers to sweep (optionable true or not yet probed unless include_unoptionable)."""
    sql = "SELECT symbol FROM symbols WHERE in_universe"
    if not include_unoptionable:
        sql += " AND optionable IS NOT false"
    return [r["symbol"] for r in query(sql + " ORDER BY market_cap DESC NULLS LAST, symbol")]


def search_symbols(q: str, limit: int = 20) -> list[dict]:
    """Prefix match on symbol first, then name contains -> [{symbol, name, kind}] (max limit; a US listing and its
    Toronto one, e.g. RY and RY.TO, are separate results)."""
    q = q.strip().upper()
    if not q:
        return []
    return query("""
        SELECT symbol, name, kind FROM symbols
        WHERE starts_with(symbol, ?) OR contains(upper(name), ?)
        ORDER BY CASE WHEN symbol = ? THEN 0 WHEN starts_with(symbol, ?) THEN 1 ELSE 2 END,
                 in_universe DESC, market_cap DESC NULLS LAST, symbol
        LIMIT ?""", [q, q, q, q, limit])


def counts() -> dict[str, int]:
    """-> {universe_size, optionable_count (a chain was stored), no_options_count (a chain source confirmed no
    listed options), total}."""
    r = query("""SELECT count(*) FILTER (WHERE in_universe) AS universe_size,
                        count(*) FILTER (WHERE in_universe AND optionable) AS optionable_count,
                        count(*) FILTER (WHERE in_universe AND optionable = false) AS no_options_count,
                        count(*) AS total FROM symbols""")[0]
    return {k: int(v) for k, v in r.items()}


def by_industry(industry: str, limit: int = 10) -> list[dict]:
    """Symbols rows of one industry, largest market cap first (peer comps)."""
    return query("""SELECT * FROM symbols WHERE industry = ? ORDER BY market_cap DESC NULLS LAST, symbol
                    LIMIT ?""", [industry, limit])
