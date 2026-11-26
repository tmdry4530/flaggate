#!/usr/bin/env bash
# Seed + smoke test the local self-hosted Flagsmith (Admin API + official Python SDK). Idempotent.
# Same env vars as selfhost-up.sh: FLAGSMITH_RUNTIME, API_PORT (default 8600), FLAGSMITH_LOGDIR.
set -euo pipefail
RUNTIME="${FLAGSMITH_RUNTIME:-$(cd "$(dirname "$0")/../.." && pwd)/flagsmith-runtime}"  # default: sibling of the repo
API_PORT="${API_PORT:-8600}"
LOGDIR="${FLAGSMITH_LOGDIR:-$RUNTIME/logs}"
export FLAGSMITH_RUNTIME="$RUNTIME" FLAGSMITH_API_URL="${FLAGSMITH_API_URL:-http://127.0.0.1:$API_PORT}"
mkdir -p "$LOGDIR"
"$RUNTIME/venv/bin/python" "$(dirname "$0")/seed.py" 2>&1 | tee "$LOGDIR/seed.log"
