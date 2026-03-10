#!/usr/bin/env bash
# Run kiso bash tests (kiso-host.sh + install.sh) using bats-core.
# Install bats: npm install -g bats  |  or: sudo apt-get install bats
set -euo pipefail

# Signal handling convention:
# - Ctrl+C must NOT close the terminal. Use `exit 130`, never `kill -INT $$`.
# - Use EXIT trap for cleanup (temp files, backups).
# - Use INT trap only for a graceful message + exit 130.
trap 'exit 130' INT

BATS=$(command -v bats 2>/dev/null || true)
if [[ -z "$BATS" ]]; then
    echo "Error: bats not found." >&2
    echo "  Install with: npm install -g bats" >&2
    exit 1
fi

cd "$(dirname "$0")"
"$BATS" tests/bash/ "$@"
