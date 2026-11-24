"""halt_status: whether trading in a symbol is halted, as the quotes report it (Questrade isHalted), and when that
state last changed (halt / resume transitions feed the `halted` / `resumed` alerts)."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

from .. import db
from ._sql import query, utc_naive


def record(observations: Iterable[tuple[str, bool, datetime]], source: str) -> list[str]:
    """Store (symbol, halted, observed_at) observations; an observation older than the stored one is ignored.
    `changed_at` becomes the observation's time when the state flips (a first observation that is halted counts
    as a change: the halt is news). Returns the symbols whose state changed."""
    obs = {s: (h, t) for s, h, t in sorted(observations, key=lambda o: o[2])}
    if not obs:
        return []
    stored = current(list(obs))
    changed, rows = [], []
    for sym, (halted, at) in obs.items():
        old = stored.get(sym)
        if old is not None and old["observed_at"] > at:
            continue
        flipped = old["halted"] != halted if old is not None else halted
        changed_at = at if flipped else (old["changed_at"] if old is not None else None)
        if flipped:
            changed.append(sym)
        rows.append([sym, halted, utc_naive(changed_at), utc_naive(at), source])
    if rows:
        with db.write() as cur:
            cur.executemany("INSERT OR REPLACE INTO halt_status VALUES (?, ?, ?, ?, ?)", rows)
    return changed


def current(symbols: Sequence[str] | None = None) -> dict[str, dict]:
    """{symbol: {halted, changed_at, observed_at, source}} (times aware UTC)."""
    syms = None if symbols is None else list(symbols)
    return {r["symbol"]: r for r in query(
        "SELECT symbol, halted, changed_at, observed_at, source FROM halt_status "
        "WHERE ?::VARCHAR[] IS NULL OR symbol IN (SELECT unnest(?::VARCHAR[]))", [syms, syms])}
