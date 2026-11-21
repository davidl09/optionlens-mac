"""The engine as the desktop app's local sidecar (docs/INTEGRATION.md §1): uvicorn on loopback, one worker
(DuckDB is single-process). Once the app is started and the socket is bound, `LISTENING <port>` is
printed on stdout (port 0 binds a free port; logs go to stderr).

Stopping, the same on macOS, Linux and Windows:
- With `exit_on_stdin_eof` (the app's `serve --exit-on-stdin-eof`) the engine shuts down gracefully when its
  stdin reaches end-of-file. The app holds the write end of that pipe and closes it to stop the engine; the
  OS closes it when the app dies. Windows has no SIGTERM (Node's kill() there is TerminateProcess, no
  cleanup), so this is the app's one graceful stop on every OS.
- With a parent pid, the engine also shuts down when that process is gone (watchdog, polled every second).
- SIGTERM/SIGINT (uvicorn's handlers) still shut it down gracefully on POSIX, e.g. from a terminal.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import threading
from typing import BinaryIO, Callable, Protocol

import uvicorn
from starlette.types import ASGIApp

from .config import Settings

log = logging.getLogger("optionlens.sidecar")
PARENT_POLL_S = 1.0


def process_alive(pid: int) -> bool:
    """Whether a process with this pid exists (signal 0 probe). POSIX only: on Windows os.kill(pid, 0)
    terminates the process instead of probing it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # exists, owned by someone else
        return True
    return True


class ParentWatch(Protocol):
    pid: int

    def alive(self) -> bool: ...


class PosixParent:
    def __init__(self, pid: int):
        self.pid = pid

    def alive(self) -> bool:
        return process_alive(self.pid)


class WindowsParent:
    """The parent as a process handle opened once at startup (SYNCHRONIZE access). A handle keeps referring
    to that one process, so a pid that Windows hands to a new process later never looks like the app.
    Alive while a zero-timeout wait on the handle times out; a handle that could not be opened: gone."""
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102

    def __init__(self, pid: int, kernel32=None):
        self.pid = pid
        self.k = kernel32 if kernel32 is not None else _kernel32()
        self.handle = self.k.OpenProcess(self.SYNCHRONIZE, False, pid)
        if not self.handle:
            log.warning("parent process %d could not be opened (gone already?)", pid)

    def alive(self) -> bool:
        return bool(self.handle) and self.k.WaitForSingleObject(self.handle, 0) == self.WAIT_TIMEOUT


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.WaitForSingleObject.restype = wintypes.DWORD
    return k


def parent_watch(pid: int, platform: str = sys.platform) -> ParentWatch:
    return WindowsParent(pid) if platform == "win32" else PosixParent(pid)


def watch_stdin(stream: BinaryIO, on_eof: Callable[[], None]) -> None:
    """Reads (and discards) `stream` until end-of-file or a read error, then calls `on_eof`. Blocking:
    run it in a daemon thread."""
    try:
        while stream.read(4096):
            pass
    except (OSError, ValueError):   # closed / broken pipe: the app is gone as well
        pass
    on_eof()


def bound_port(servers: list[asyncio.base_events.Server]) -> int:
    """TCP port of the first listening socket."""
    return next(s.getsockname()[1] for srv in servers for s in srv.sockets
                if s.family in (socket.AF_INET, socket.AF_INET6))


class SidecarServer(uvicorn.Server):
    """uvicorn.Server that announces its port and watches the parent process."""

    def __init__(self, config: uvicorn.Config, parent: ParentWatch | None = None, out=None):
        super().__init__(config)
        self.parent, self.out = parent, out
        self._watchdog: asyncio.Task | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.should_exit:
            return
        print(f"LISTENING {bound_port(self.servers)}", file=self.out, flush=True)
        if self.parent is not None:
            self._watchdog = asyncio.create_task(self.watch_parent())

    async def watch_parent(self) -> None:
        while not self.should_exit:
            if not self.parent.alive():
                log.warning("parent process %d is gone: shutting down", self.parent.pid)
                self.should_exit = True
                return
            await asyncio.sleep(PARENT_POLL_S)

    def request_exit(self, why: str) -> None:
        """Graceful shutdown, callable from any thread (uvicorn checks should_exit every 0.1 s)."""
        log.info("%s: shutting down", why)
        self.should_exit = True


def server_config(app: ASGIApp, settings: Settings) -> uvicorn.Config:
    """uvicorn config of the sidecar: one worker on loopback, no access log (the app polls the API all day
    and keeps engine output in its own log; per-request lines only made that log grow)."""
    return uvicorn.Config(app, host=settings.host, port=settings.port, workers=1, log_level="info", access_log=False)


def serve(app: ASGIApp, settings: Settings, exit_on_stdin_eof: bool = False) -> int:
    """Run until stdin EOF (if asked), parent death or SIGTERM/SIGINT. Returns the process exit code."""
    parent = parent_watch(settings.parent_pid) if settings.parent_pid is not None else None
    server = SidecarServer(server_config(app, settings), parent)
    if exit_on_stdin_eof:
        threading.Thread(target=watch_stdin, args=(sys.stdin.buffer, lambda: server.request_exit("stdin closed")),
                         name="stdin-eof", daemon=True).start()
    server.run()
    return 0
