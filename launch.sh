#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
else
    echo "ERROR: $REPO_ROOT/.env not found"
    exit 1
fi

RESOLVED=$(envsubst < "$SCRIPT_DIR/launch-config.json")
TMPFILE=$(mktemp /tmp/launch-config-XXXX.json)
echo "$RESOLVED" > "$TMPFILE"
trap "rm -f $TMPFILE" EXIT

exec plato chronos launch "$TMPFILE" "$@"
