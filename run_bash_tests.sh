#!/usr/bin/env bash
# Run kiso bash tests (kiso-host.sh + install.sh) using bats-core.
# Install bats: npm install -g bats  |  or: sudo apt-get install bats
set -euo pipefail

BATS=$(command -v bats 2>/dev/null || true)
if [[ -z "$BATS" ]]; then
    echo "Error: bats not found." >&2
    echo "  Install with: npm install -g bats" >&2
    exit 1
fi

cd "$(dirname "$0")"
exec "$BATS" tests/bash/ "$@"
