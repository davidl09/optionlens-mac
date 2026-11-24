"""The user's choice of how Questrade keeps the screen live (docs/INTEGRATION.md §9): "stream" (Level 1 WebSocket
streams) or "poll" (snapshots at a cadence). Saved in DATA_DIR/live_mode.json once the user chooses; until then the
default is OPTIONLENS_QUESTRADE_LIVE (itself "poll" unless set), because Questrade documents that streaming freezes
the market data of any other IQ platform the user has open."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

from ..fileio import read_json, write_json_atomic

Mode = Literal["poll", "stream"]
MODES: tuple[str, ...] = get_args(Mode)
FILE_NAME = "live_mode.json"


class ModeSetting:
    def __init__(self, path: Path, default: Mode):
        if default not in MODES:
            raise ValueError(f"live mode must be one of {MODES}, not {default!r}")
        self.path, self.default = path, default

    def _saved(self) -> Mode | None:
        rec = read_json(self.path)
        mode = rec.get("mode") if isinstance(rec, dict) else None
        return mode if mode in MODES else None

    def get(self) -> Mode:
        return self._saved() or self.default

    def set(self, mode: Mode) -> None:
        """Save the user's choice. Raises ValueError for an unknown mode, OSError when it cannot be written."""
        if mode not in MODES:
            raise ValueError(f"live mode must be one of {MODES}, not {mode!r}")
        write_json_atomic(self.path, {"mode": mode})

    def view(self) -> dict:
        """{mode, default, saved}: what is in effect, the default without a choice, and whether the user chose."""
        saved = self._saved()
        return {"mode": saved or self.default, "default": self.default, "saved": saved is not None}
