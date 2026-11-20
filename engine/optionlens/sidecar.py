"""The engine as the Mac app's local sidecar (docs/INTEGRATION.md §1): uvicorn on loopback, one worker
(DuckDB is single-process). Once the app is started and the socket is bound, `LISTENING <port>` is
printed on stdout (port 0 binds a free port; logs go to stderr). SIGTERM/SIGINT shut down gracefully
(uvicorn's handlers); with a parent pid, the engine also shuts down when that process disappears."""
from __future__ import annotations

import asyncio
import logging
import os
import socket

import uvicorn
from starlette.types import ASGIApp

from .config import Settings

log = logging.getLogger("optionlens.sidecar")
PARENT_POLL_S = 1.0


def process_alive(pid: int) -> bool:
    """Whether a process with this pid exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # exists, owned by someone else
        return True
    return True


def bound_port(servers: list[asyncio.base_events.Server]) -> int:
    """TCP port of the first listening socket."""
    return next(s.getsockname()[1] for srv in servers for s in srv.sockets
                if s.family in (socket.AF_INET, socket.AF_INET6))


class SidecarServer(uvicorn.Server):
    """uvicorn.Server that announces its port and watches the parent process."""

    def __init__(self, config: uvicorn.Config, parent_pid: int | None = None, out=None):
        super().__init__(config)
        self.parent_pid, self.out = parent_pid, out
        self._watchdog: asyncio.Task | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.should_exit:
            return
        print(f"LISTENING {bound_port(self.servers)}", file=self.out, flush=True)
        if self.parent_pid is not None:
            self._watchdog = asyncio.create_task(self.watch_parent())

    async def watch_parent(self) -> None:
        while not self.should_exit:
            if not process_alive(self.parent_pid):
                log.warning("parent process %d is gone: shutting down", self.parent_pid)
                self.should_exit = True
                return
            await asyncio.sleep(PARENT_POLL_S)


def server_config(app: ASGIApp, settings: Settings) -> uvicorn.Config:
    """uvicorn config of the sidecar: one worker on loopback, no access log (the app polls the API all day
    and keeps engine output in its own log; per-request lines only made that log grow)."""
    return uvicorn.Config(app, host=settings.host, port=settings.port, workers=1, log_level="info", access_log=False)


def serve(app: ASGIApp, settings: Settings) -> int:
    """Run until SIGTERM/SIGINT or parent death. Returns the process exit code."""
    SidecarServer(server_config(app, settings), settings.parent_pid).run()
    return 0
