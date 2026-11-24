"""The user's portfolio settings (DATA_DIR/portfolio.json) and the brokers' latest snapshots (memory only: positions
and balances are never written to disk; a restart fetches them again).

Settings: per broker "Show my positions" (`questrade`, `ibkr`): unset = on (the default once the broker supplies
data), false = hidden and not fetched (no Questrade account call, the IBKR bridge stops its positions feed)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..fileio import read_json, write_json_atomic
from .model import BROKERS, BrokerSnapshot

FILE_NAME = "portfolio.json"


class PortfolioSettings:
    def __init__(self, path: Path):
        self.path = path

    def _raw(self) -> dict:
        raw = read_json(self.path)
        return raw if isinstance(raw, dict) else {}

    def view(self) -> dict:
        """{questrade: bool, ibkr: bool} (effective: unset = on)."""
        raw = self._raw()
        return {b: raw.get(b) is not False for b in BROKERS}

    def enabled(self, broker: str) -> bool:
        return self.view()[broker]

    def set(self, changes: dict[str, bool]) -> dict:
        """Save the given brokers' switches (others unchanged). Raises OSError."""
        raw = {**self._raw(), **{b: bool(v) for b, v in changes.items() if b in BROKERS}}
        write_json_atomic(self.path, raw)
        return self.view()


@dataclass
class BrokerState:
    """One broker's last outcome: `state` ok | needs_permission | not_connected | error | disabled | waiting;
    `detail` what to do / what failed (never an account number); `snapshot` the last good data."""
    state: str = "waiting"
    detail: str | None = None
    snapshot: BrokerSnapshot | None = None
    attempted_at: datetime | None = None


@dataclass
class Snapshots:
    _by: dict[str, BrokerState] = field(default_factory=lambda: {b: BrokerState() for b in BROKERS})
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, broker: str) -> BrokerState:
        with self._lock:
            s = self._by[broker]
            return BrokerState(s.state, s.detail, s.snapshot, s.attempted_at)

    def ok(self, snap: BrokerSnapshot) -> None:
        with self._lock:
            self._by[snap.broker] = BrokerState("ok", None, snap, snap.fetched_at)

    def failed(self, broker: str, state: str, detail: str | None, at: datetime, keep: bool = True) -> None:
        """A failed attempt: the last good snapshot is kept (shown as of its time) unless `keep` is false."""
        with self._lock:
            s = self._by[broker]
            self._by[broker] = BrokerState(state, detail, s.snapshot if keep else None, at)

    def clear(self, broker: str, state: str = "disabled") -> None:
        with self._lock:
            self._by[broker] = BrokerState(state)
