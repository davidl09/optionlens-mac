#!/usr/bin/env bash
# Starts the engine with the Python bundled inside a packaged app, exactly as the app does
# (python -m optionlens.cli serve --port 0), waits for `LISTENING <port>`, checks /api/health, stops it.
# Usage: scripts/smoke-bundled-engine.sh <path to the bundled python3> [timeout seconds]
set -euo pipefail
PY="$1"
TIMEOUT="${2:-90}"
[ -x "$PY" ] || { echo "not an executable: $PY" >&2; exit 1; }
DATA="$(mktemp -d)"
OUT="$DATA/stdout"
trap 'kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; rm -rf "$DATA"' EXIT
env -u PYTHONHOME -u PYTHONPATH PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1 OPTIONLENS_DATA_DIR="$DATA" OPTIONLENS_NO_SCHEDULER=1 \
  OPTIONLENS_API_TOKEN="smoke-test-token-0123456789" "$PY" -m optionlens.cli serve --port 0 >"$OUT" 2>"$DATA/stderr" &
PID=$!
for _ in $(seq 1 "$((TIMEOUT * 2))"); do
  PORT="$(sed -n 's/^LISTENING \([0-9]*\)$/\1/p' "$OUT" | head -n 1)"
  [ -n "$PORT" ] && break
  kill -0 "$PID" 2>/dev/null || { echo "engine exited before LISTENING:" >&2; cat "$DATA/stderr" >&2; exit 1; }
  sleep 0.5
done
[ -n "${PORT:-}" ] || { echo "no LISTENING within ${TIMEOUT}s:" >&2; cat "$DATA/stderr" >&2; exit 1; }
HEALTH="$(curl -fsS "http://127.0.0.1:$PORT/api/health")"
echo "bundled engine LISTENING on $PORT: $HEALTH"
case "$HEALTH" in *'"ok":true'*) ;; *) echo "unexpected health response" >&2; exit 1 ;; esac
