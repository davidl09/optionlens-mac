"""What the user is looking at (docs/INTEGRATION.md §10 "Current view"): the renderer reports its view on navigation
and changes (route, symbol, tab, report, table state, visible rows, and the engine requests the page shows); the MCP
tool `get_current_view` reads it back and re-runs those requests. Memory only: the latest report wins."""
from __future__ import annotations

import time
from datetime import datetime, timezone

_current: dict | None = None
_at: float | None = None


def report(view: dict, now: float | None = None) -> None:
    global _current, _at
    _current, _at = view, (time.time() if now is None else now)


def current(now: float | None = None) -> dict:
    """{view, reported_at, age_s}: view None until the renderer reported one."""
    if _current is None or _at is None:
        return {"view": None, "reported_at": None, "age_s": None}
    t = time.time() if now is None else now
    return {"view": _current, "reported_at": datetime.fromtimestamp(_at, timezone.utc).isoformat(),
            "age_s": round(max(0.0, t - _at), 1)}


def clear() -> None:
    global _current, _at
    _current, _at = None, None
