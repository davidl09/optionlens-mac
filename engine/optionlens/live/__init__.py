"""Live quotes (docs/INTEGRATION.md §9): what the renderer windows show is kept live from Questrade (polled
snapshots, or Level 1 streams when enabled), from IBKR bridge snapshots when Questrade is unavailable, and pushed to
the windows as coalesced patches over Server-Sent Events.

- subscriptions: Wanted / Registry / union / diff (pure);
- session: regular / extended / closed (pure);
- hub: LiveHub, the subsystem's contract (subscribe, events, publish, bridge_wants, status);
- questrade: the Questrade driver (polling cadence from the rate gate, or streams);
- stream: the Questrade Level 1 WebSocket client;
- mode: the user's choice of streaming or polling (saved in DATA_DIR);
- resolve / persist: the database side (chain views -> contracts; live values -> quotes_latest / option_latest).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models import IntradayBar
from .hub import LiveHub
from .mode import FILE_NAME as MODE_FILE
from .mode import Mode, ModeSetting
from .subscriptions import Wanted

__all__ = ["LiveHub", "Wanted", "instance", "mode_view", "pending_bars", "set_mode", "start", "status", "stop"]

BRIDGE_CHECK_S = 10.0
_hub: LiveHub | None = None
_mode: ModeSetting | None = None


def _mode_setting() -> ModeSetting:
    """The user's live mode, saved in DATA_DIR (the env var OPTIONLENS_QUESTRADE_LIVE is only its default)."""
    global _mode
    from ..config import get_settings
    s = get_settings()
    path = s.data_dir / MODE_FILE
    if _mode is None or _mode.path != path or _mode.default != s.questrade_live:
        _mode = ModeSetting(path, s.questrade_live)  # type: ignore[arg-type]
    return _mode


def mode_view() -> dict:
    """{mode, default, saved} (GET /api/live/mode)."""
    return _mode_setting().view()


def set_mode(mode: Mode) -> dict:
    """Save the user's live mode and put it into effect at once (no restart). Raises ValueError / OSError."""
    _mode_setting().set(mode)
    instance().set_mode(mode)
    return mode_view()


def status() -> dict:
    """The live status plus the Questrade driver's details (GET /api/live/status)."""
    h = instance()
    return {**h.status(), "questrade": h.diagnostics()}


def _questrade_driver():
    from ..sources import chain_source
    from .questrade import QuestradeLive
    if "questrade" not in chain_source.server_source_names():
        return None
    return QuestradeLive(chain_source.REGISTRY["questrade"](), instance().publish, mode=_mode_setting().get())


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
        from .persist import persist, store_bars
        _hub = LiveHub(_questrade_driver, _BridgeCheck(), persist, store_bars=store_bars)
    return _hub


def pending_bars(symbol: str) -> list[IntradayBar]:
    """The live 1m bars of `symbol` not stored yet (IntradayBar, ascending); none without a running hub."""
    return _hub.pending_bars(symbol) if _hub is not None else []


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
