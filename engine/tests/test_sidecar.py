"""The engine as the app's sidecar: loopback-only settings, `serve --port 0` announcing its port,
graceful SIGTERM and the parent-death watchdog (real subprocesses on a temporary DATA_DIR)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

from optionlens import config
from optionlens.sidecar import process_alive

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "s" * 32


@pytest.mark.parametrize("env,msg", [
    ({"OPTIONLENS_HOST": "0.0.0.0"}, "must be loopback"),
    ({"OPTIONLENS_HOST": "192.168.1.5"}, "must be loopback"),
    ({"OPTIONLENS_PORT": "70000"}, "0..65535"),
    ({"OPTIONLENS_API_TOKEN": "short"}, "at least 16"),
    ({"OPTIONLENS_PARENT_PID": "abc"}, "not a valid int"),
])
def test_settings_fail_fast(monkeypatch, tmp_path, env, msg):
    monkeypatch.setenv("OPTIONLENS_DATA_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError, match=msg):
        config.load_settings()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_settings(monkeypatch, tmp_path, host):
    monkeypatch.setenv("OPTIONLENS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPTIONLENS_HOST", host)
    monkeypatch.setenv("OPTIONLENS_PORT", "0")
    monkeypatch.setenv("OPTIONLENS_PARENT_PID", "4242")
    monkeypatch.setenv("OPTIONLENS_CORS_ORIGINS", "app://optionlens, http://localhost:5173")
    s = config.load_settings()
    assert s.port == 0 and s.parent_pid == 4242 and not s.auth_enabled
    assert s.cors_origins == ("app://optionlens", "http://localhost:5173")
    assert s.chain_universe_size == 150 and s.chain_source == "none"


def test_process_alive():
    assert process_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not process_alive(p.pid)


def _start(tmp_path, **env) -> tuple[subprocess.Popen, int]:
    e = {**os.environ, "OPTIONLENS_DATA_DIR": str(tmp_path), "OPTIONLENS_NO_SCHEDULER": "1",
         "OPTIONLENS_API_TOKEN": TOKEN, **env}
    p = subprocess.Popen([sys.executable, "-m", "optionlens.cli", "serve", "--port", "0"], cwd=SERVER_DIR, env=e,
                         stdout=subprocess.PIPE, stderr=open(tmp_path / "engine.log", "w"), text=True)
    line = p.stdout.readline()
    assert line.startswith("LISTENING "), line
    return p, int(line.split()[1])


def _wait(p: subprocess.Popen, timeout: float = 15) -> int:
    try:
        return p.wait(timeout)
    finally:
        if p.poll() is None:
            p.kill()


def test_serve_port_zero_announces_port_and_stops_on_sigterm(tmp_path):
    p, port = _start(tmp_path)
    try:
        base = f"http://127.0.0.1:{port}"
        h = httpx.get(f"{base}/api/health").json()
        assert h["ok"] and h["auth"] is True
        assert httpx.get(f"{base}/api/status").status_code == 401
        assert httpx.get(f"{base}/api/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    finally:
        p.send_signal(signal.SIGTERM)
    assert _wait(p) == -signal.SIGTERM          # uvicorn re-raises the signal after its graceful shutdown
    log = (tmp_path / "engine.log").read_text()
    assert "Application shutdown complete" in log
    # no per-request access log (uvicorn writes it to stdout, which the app copies into main.log)
    assert "GET /api/status" not in log + p.stdout.read()


def test_engine_exits_when_parent_dies(tmp_path):
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    p, _ = _start(tmp_path, OPTIONLENS_PARENT_PID=str(parent.pid))
    time.sleep(0.5)
    assert p.poll() is None
    parent.kill()
    parent.wait()
    assert _wait(p) == 0
    log = (tmp_path / "engine.log").read_text()
    assert f"parent process {parent.pid} is gone" in log and "Application shutdown complete" in log
