"""Starts the engine with the interpreter running this script (the Python bundled in a packaged app) exactly
as the app does: `python -m optionlens.cli serve --port 0 --exit-on-stdin-eof`, stdin a pipe, data in a
folder whose name has a space. Waits for `LISTENING <port>`, checks /api/health, then closes the engine's
stdin and checks that it shuts down cleanly (exit code 0, "Application shutdown complete"): the app's stop
on every OS. Standard library only; run with -I -B (no user site, no bytecode written into the app).
Usage: <bundled python> -I -B scripts/smoke-bundled-engine.py [timeout seconds]"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
with tempfile.TemporaryDirectory(prefix="optionlens smoke ") as tmp:
    data = os.path.join(tmp, "data dir")
    os.mkdir(data)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", OPTIONLENS_DATA_DIR=data,
               OPTIONLENS_NO_SCHEDULER="1", OPTIONLENS_API_TOKEN="smoke-test-token-0123456789")
    with open(os.path.join(tmp, "stderr"), "w+", encoding="utf-8", errors="replace") as err:
        p = subprocess.Popen([sys.executable, "-m", "optionlens.cli", "serve", "--port", "0", "--exit-on-stdin-eof"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, env=env, text=True)
        lines: list[str] = []
        threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True).start()

        def fail(msg: str) -> None:
            p.kill()
            err.seek(0)
            sys.exit(f"{msg}\n{err.read()}")

        deadline = time.monotonic() + timeout
        port = None
        while port is None:
            port = next((int(m.group(1)) for m in map(re.compile(r"^LISTENING (\d+)\s*$").match, list(lines)) if m), None)
            if port is None and p.poll() is not None:
                fail(f"engine exited (code {p.returncode}) before LISTENING:")
            if port is None and time.monotonic() > deadline:
                fail(f"no LISTENING within {timeout:.0f} s:")
            time.sleep(0.2)
        health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=10))
        print(f"bundled engine LISTENING on {port}: {json.dumps(health)}")
        if health.get("ok") is not True:
            fail("unexpected health response")
        p.stdin.close()   # the app's stop
        try:
            code = p.wait(30)
        except subprocess.TimeoutExpired:
            fail("engine did not exit within 30 s after its stdin was closed:")
        err.seek(0)
        log = err.read()
        if code != 0 or "Application shutdown complete" not in log:
            fail(f"engine did not shut down cleanly (exit code {code}):")
        if not os.path.exists(os.path.join(data, "optionlens.duckdb")):
            fail("no database in the data folder:")
        print("closed stdin: engine shut down cleanly (exit code 0)")
