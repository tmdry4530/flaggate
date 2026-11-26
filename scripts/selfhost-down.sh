#!/usr/bin/env bash
# Stop only the processes selfhost-up.sh started (by recorded PID / pg_ctl), never pkill.
set -euo pipefail
RUNTIME="${FLAGSMITH_RUNTIME:-../flagsmith-runtime}"
PGBIN="$RUNTIME/pgconda/bin"
if [ -f "$RUNTIME/api.pid" ]; then kill "$(cat "$RUNTIME/api.pid")" 2>/dev/null || true; rm -f "$RUNTIME/api.pid"; fi
"$PGBIN/pg_ctl" -D "$RUNTIME/pgdata" stop -m fast 2>/dev/null || true
