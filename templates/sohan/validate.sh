#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

schema_only=false
if [[ "${1:-}" == "--schema-only" ]]; then
  schema_only=true
fi

check() {
  local label="$1"
  shift
  echo "Running ${label}..."
  if "$@"; then
    echo "✓ ${label} passed"
  else
    echo "✗ ${label} failed" >&2
    exit 1
  fi
}

# Ensure bun is available
if ! command -v bun >/dev/null 2>&1; then
  echo "Installing bun..."
  curl -fsSL https://bun.sh/install | bash
  export BUN_INSTALL="$HOME/.bun"
  export PATH="$BUN_INSTALL/bin:$PATH"
  ln -sf "$BUN_INSTALL/bin/bun" /usr/local/bin/bun
fi

if [[ ! -x "web/node_modules/.bin/eslint" ]]; then
  echo "Installing web dependencies..."
  (cd web && bun install)
fi

check "eslint" bash -lc 'cd web && bun run lint -- --max-warnings=0'
check "typecheck" bash -lc 'cd web && bun run typecheck'
if [[ "$schema_only" != "true" ]]; then
  check "vitest (coverage)" bash -lc 'cd web && bun run test:coverage'
fi

echo "✅ Validation passed"
