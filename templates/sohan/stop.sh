#!/usr/bin/env bash
# Idempotent — safe to call multiple times or when services are already stopped.
set -euo pipefail
cd "$(dirname "$0")"

ROOT_DIR=$(pwd)
APP_PORT=${APP_PORT:-3000}
RUNTIME_DIR="$ROOT_DIR/.runtime/${APP_PORT}"

# Source runtime env to pick up the actual PGLITE_DATA_DIR used by start.sh
if [[ -f "$RUNTIME_DIR/runtime.env" ]]; then
  source "$RUNTIME_DIR/runtime.env"
fi

# Kill the web process if it's still running
if [[ -f "$RUNTIME_DIR/web.pid" ]]; then
  WEB_PID=$(cat "$RUNTIME_DIR/web.pid" 2>/dev/null || true)
  if [[ -n "${WEB_PID:-}" ]] && kill -0 "$WEB_PID" 2>/dev/null; then
    # Kill the entire process group to catch child processes
    kill -- -"$WEB_PID" 2>/dev/null || kill "$WEB_PID" 2>/dev/null || true
    # Don't use wait — the process may not be a child of this shell
    for _ in $(seq 1 30); do
      kill -0 "$WEB_PID" 2>/dev/null || break
      sleep 0.1
    done
  fi
  rm -f "$RUNTIME_DIR/web.pid"
fi

# Serialize PGlite data to a snapshot in the code workspace
PGLITE_DATA_DIR="${PGLITE_DATA_DIR:-/tmp/pglite-data/${APP_PORT}}"
PGLITE_SNAPSHOT="$ROOT_DIR/.runtime/pglite-snapshot.tar.gz"
if [[ -d "$PGLITE_DATA_DIR" && -n "$(ls -A "$PGLITE_DATA_DIR" 2>/dev/null)" ]]; then
  echo "Saving PGlite database snapshot..."
  mkdir -p "$(dirname "$PGLITE_SNAPSHOT")"
  tar -czf "$PGLITE_SNAPSHOT" -C "$PGLITE_DATA_DIR" .
fi

rm -f "$RUNTIME_DIR/server.json"

echo "✅ Services stopped (port $APP_PORT)"
