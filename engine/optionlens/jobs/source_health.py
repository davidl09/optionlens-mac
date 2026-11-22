"""How the server-side chain sources' fetches went (in memory, since the engine started): grouped failure reasons
for the status ("1,141 × 403 code 1016 (missing market-data permission)"), whether a source is failing (the
IBKR bridge then takes over its chain work, bridge_plan), and the last result per symbol (coverage of sweeps and
after-hours fills: a failed attempt is never coverage).

Results: "ok" (a chain stored), "no_options" (the source lists no options for the symbol), "not_listed" (the source
does not carry it), "failed" (with a reason)."""
from __future__ import annotations

import threading
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

Result = Literal["ok", "no_options", "not_listed", "failed"]

WINDOW = timedelta(hours=24)   # failure reasons counted over this window
RECENT = 50                    # attempts that decide whether a source is failing
FAILING_MIN = 10               # ... once at least this many were made
FAILING_SHARE = 0.5            # failing: more than this share of the recent attempts failed
MAX_EVENTS = 50_000


@dataclass(frozen=True)
class Attempt:
    result: Result
    at: datetime
    reason: str | None = None


class _Health:
    def __init__(self) -> None:
        self.failures: deque[tuple[datetime, str]] = deque(maxlen=MAX_EVENTS)
        self.recent: deque[bool] = deque(maxlen=RECENT)
        self.last: dict[str, Attempt] = {}


_by_source: dict[str, _Health] = {}
_lock = threading.Lock()


def _h(source: str) -> _Health:
    h = _by_source.get(source)
    if h is None:
        h = _by_source[source] = _Health()
    return h


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def record(source: str, symbol: str, result: Result, reason: str | None = None, now: datetime | None = None) -> None:
    """Note one fetch of `symbol` from `source` (`reason`: the failure's grouping key)."""
    t = _now(now)
    with _lock:
        h = _h(source)
        h.last[symbol] = Attempt(result, t, reason if result == "failed" else None)
        if result == "failed":
            h.failures.append((t, reason or "unknown error"))
        if result != "not_listed":
            h.recent.append(result != "failed")


def last(source: str, symbol: str) -> Attempt | None:
    with _lock:
        return _h(source).last.get(symbol)


def top_failures(source: str, n: int = 3, now: datetime | None = None) -> list[dict]:
    """[{reason, count}] of the most frequent failure reasons within WINDOW, most frequent first."""
    since = _now(now) - WINDOW
    with _lock:
        counts = Counter(r for t, r in _h(source).failures if t >= since)
    return [{"reason": r, "count": c} for r, c in counts.most_common(n)]


def failing(source: str) -> bool:
    """More than FAILING_SHARE of the last RECENT attempts failed (with at least FAILING_MIN attempts)."""
    with _lock:
        recent = list(_h(source).recent)
    return len(recent) >= FAILING_MIN and recent.count(False) > FAILING_SHARE * len(recent)


def reset(source: str) -> None:
    """Forget a source's history (a new token was saved: earlier failures say nothing about it)."""
    with _lock:
        _by_source.pop(source, None)


def format_failures(failures: list[dict]) -> str:
    """'1,141 × 403 code 1016 (missing market-data permission); 3 × …'."""
    return "; ".join(f"{f['count']:,} × {f['reason']}" for f in failures)
