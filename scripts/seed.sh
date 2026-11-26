#!/usr/bin/env bash
# Seed + smoke test the local self-hosted Flagsmith (Admin API + official Python SDK). Idempotent.
set -euo pipefail
RUNTIME="${FLAGSMITH_RUNTIME:-../flagsmith-runtime}"
export FLAGSMITH_RUNTIME="$RUNTIME" FLAGSMITH_API_URL="${FLAGSMITH_API_URL:-http://127.0.0.1:8600}"
mkdir -p "$RUNTIME/logs"
"$RUNTIME/venv/bin/python" "$(dirname "$0")/seed.py" 2>&1 | tee "$RUNTIME/logs/seed.log"
