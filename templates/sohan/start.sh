#!/usr/bin/env bash
set -euo pipefail

# Increase file descriptor limit for PGlite and Next.js
ulimit -n 65000 2>/dev/null || true

cd "$(dirname "$0")"

ROOT_DIR=$(pwd)
WEB_DIR="$ROOT_DIR/web"
APP_PORT=${APP_PORT:-3000}
RUNTIME_DIR="$ROOT_DIR/.runtime/${APP_PORT}"
LOG_DIR="$RUNTIME_DIR/logs"
RUNTIME_ENV="$RUNTIME_DIR/runtime.env"
PGLITE_DATA_DIR="${PGLITE_DATA_DIR:-/tmp/pglite-data/${APP_PORT}}"

check_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required tool '$1' not found" >&2
    exit 1
  fi
}

ensure_bunx() {
  if command -v bunx >/dev/null 2>&1; then
    return 0
  fi

  cat > /usr/local/bin/bunx <<'EOF'
#!/usr/bin/env bash
exec bun x "$@"
EOF
  chmod +x /usr/local/bin/bunx
}

node_major_version() {
  if ! command -v node >/dev/null 2>&1; then
    echo 0
    return 0
  fi
  node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0
}

install_or_upgrade_node_if_needed() {
  local major
  major=$(node_major_version)
  if command -v npm >/dev/null 2>&1 && [[ "$major" -ge 20 ]]; then
    return 0
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: Node.js/npm are missing (or too old) and sudo is unavailable" >&2
    exit 1
  fi

  if [[ "$major" -gt 0 && "$major" -lt 20 ]]; then
    echo "Node.js v${major} detected; upgrading to Node.js 22..."
  else
    echo "Installing Node.js 22..."
  fi

  sudo apt-get update -y >/dev/null
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg >/dev/null
  sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
  echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | sudo tee /etc/apt/sources.list.d/nodesource.list >/dev/null
  sudo apt-get update -y >/dev/null
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs >/dev/null

  check_tool node
  check_tool npm
  major=$(node_major_version)
  if [[ "$major" -lt 20 ]]; then
    echo "ERROR: Node.js >=20 is required but found v${major}" >&2
    exit 1
  fi
}

ensure_node_modules() {
  if [[ -x "$WEB_DIR/node_modules/.bin/next" ]]; then
    return 0
  fi

  echo "Installing web dependencies..."
  (cd "$WEB_DIR" && bun install)
}

wait_for_app() {
  local tries=0
  until curl -sf "http://127.0.0.1:${APP_PORT}/api/health" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [[ "$tries" -gt 120 ]]; then
      echo "ERROR: Next.js did not become healthy on port $APP_PORT" >&2
      tail -n 200 "$LOG_DIR/web.log" >&2 || true
      return 1
    fi
    sleep 1
  done
}

check_tool curl

mkdir -p "$LOG_DIR" "$RUNTIME_DIR"

bash stop.sh >/dev/null 2>&1 || true

if [[ -f ".env.example" && ! -f ".env" ]]; then
  cp .env.example .env
fi
if [[ -f "web/.env.example" && ! -f "web/.env.local" ]]; then
  cp web/.env.example web/.env.local
fi

install_or_upgrade_node_if_needed

# Ensure bun is available
if ! command -v bun >/dev/null 2>&1; then
  echo "Installing bun..."
  curl -fsSL https://bun.sh/install | bash
  export BUN_INSTALL="$HOME/.bun"
  export PATH="$BUN_INSTALL/bin:$PATH"
  ln -sf "$BUN_INSTALL/bin/bun" /usr/local/bin/bun
fi
ensure_bunx
check_tool bun

ensure_node_modules

# Restore PGlite data from snapshot if available, otherwise start fresh
PGLITE_SNAPSHOT="$ROOT_DIR/.runtime/pglite-snapshot.tar.gz"
rm -rf "$PGLITE_DATA_DIR"
mkdir -p "$PGLITE_DATA_DIR"
if [[ -f "$PGLITE_SNAPSHOT" ]]; then
  echo "Restoring PGlite database from snapshot..."
  tar -xzf "$PGLITE_SNAPSHOT" -C "$PGLITE_DATA_DIR"
fi

# Scope Next.js build dir per port so parallel instances don't conflict
NEXT_DIST_DIR="${NEXT_DIST_DIR:-.next-${APP_PORT}}"
# Remove default .next dir to avoid stale lock from hardlinked base workspace.
# Next.js checks .next/dev/lock before reading distDir from next.config.ts,
# so hardlinked copies inherit the base workspace's lock. Safe to remove since
# this instance uses .next-<port> via NEXT_DIST_DIR.
rm -rf "$WEB_DIR/.next"

cat > "$RUNTIME_ENV" <<RUNTIME
export PGLITE_DATA_DIR="$PGLITE_DATA_DIR"
export NEXT_DIST_DIR="$NEXT_DIST_DIR"
export APP_PORT="$APP_PORT"
RUNTIME

(
  cd "$WEB_DIR"
  PGLITE_DATA_DIR="$PGLITE_DATA_DIR" NEXT_DIST_DIR="$NEXT_DIST_DIR" \
    bun run dev -- --hostname 0.0.0.0 --port "$APP_PORT" > "$LOG_DIR/web.log" 2>&1 &
  echo $! > "$RUNTIME_DIR/web.pid"
)

wait_for_app

cat > "$RUNTIME_DIR/server.json" <<JSON
{
  "frontend_url": "http://localhost:${APP_PORT}",
  "backend_url": "http://localhost:${APP_PORT}",
  "frontend_port": ${APP_PORT},
  "backend_port": ${APP_PORT},
  "pglite_data_dir": "${PGLITE_DATA_DIR}"
}
JSON

echo "✅ Services ready"
echo "   App: http://localhost:${APP_PORT}"
echo "   DB : pglite://${PGLITE_DATA_DIR}"
