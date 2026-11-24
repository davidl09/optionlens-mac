"""Live quotes (docs/INTEGRATION.md §9): what the renderer windows show is kept live from Questrade (polled
snapshots, or Level 1 streams when enabled), from IBKR bridge snapshots when Questrade is unavailable, and pushed to
the windows as coalesced patches over Server-Sent Events.

- subscriptions: Wanted / Registry / union / diff (pure);
- session: regular / extended / closed (pure);
- hub: LiveHub, the subsystem's contract (subscribe, events, publish, bridge_wants, status);
- questrade: the Questrade driver (polling cadence from the rate gate, or streams);
- stream: the Questrade Level 1 WebSocket client;
- resolve / persist: the database side (chain views -> contracts; live values -> quotes_latest / option_latest).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .hub import LiveHub
from .subscriptions import Wanted

__all__ = ["LiveHub", "Wanted", "instance", "start", "stop"]

BRIDGE_CHECK_S = 10.0
_hub: LiveHub | None = None


def _questrade_driver():
    from ..config import get_settings
    from ..sources import chain_source
    from .questrade import QuestradeLive
    if "questrade" not in chain_source.server_source_names():
        return None
    return QuestradeLive(chain_source.REGISTRY["questrade"](), instance().publish, mode=get_settings().questrade_live)


class _BridgeCheck:
    """Whether the IBKR bridge is connected (its heartbeat, services.system.bridge), re-read every BRIDGE_CHECK_S."""

    def __init__(self) -> None:
        self._at: datetime | None = None
        self._ok = False

    def __call__(self) -> bool:
        now = datetime.now(timezone.utc)
        if self._at is None or now - self._at > timedelta(seconds=BRIDGE_CHECK_S):
            from ..services import system
            try:
                self._ok = bool(system.bridge(now)["connected"])
            except Exception:  # noqa: BLE001 - no database yet: not connected
                self._ok = False
            self._at = now
        return self._ok


def instance() -> LiveHub:
    """The process-wide LiveHub."""
    global _hub
    if _hub is None:
        from .persist import persist
        _hub = LiveHub(_questrade_driver, _BridgeCheck(), persist)
    return _hub


async def start() -> None:
    await instance().start()


def close_streams() -> None:
    """End the open live streams (the sidecar's shutdown, before uvicorn waits for open connections)."""
    if _hub is not None:
        _hub.close_streams()


async def stop() -> None:
    global _hub
    if _hub is not None:
        await _hub.stop()
        _hub = None
