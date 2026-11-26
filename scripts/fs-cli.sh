#!/usr/bin/env bash
# Run the official Flagsmith CLI (@flagsmith/cli v2, npm) against the local self-hosted API.
# Loads credentials from $RUNTIME/local.env and exports them under the host-scoped variable the CLI
# requires for self-hosted instances (http://127.0.0.1:8600 -> FLAGSMITH_API_KEY_127_0_0_1_8600).
set -euo pipefail
RUNTIME="${FLAGSMITH_RUNTIME:-$(cd "$(dirname "$0")/../.." && pwd)/flagsmith-runtime}"  # default: sibling of the repo
API_PORT="${API_PORT:-8600}"
set -a; . "$RUNTIME/local.env"; set +a
export FLAGSMITH_API_URL="http://127.0.0.1:$API_PORT"
export "FLAGSMITH_API_KEY_127_0_0_1_${API_PORT}=$FS_ADMIN_API_KEY"
export FLAGSMITH_ENVIRONMENT="$FS_ENV_KEY"
export FLAGSMITH_ORGANISATION="${FLAGSMITH_ORGANISATION:-Toggle Wars Demo}"
export FLAGSMITH_PROJECT="${FLAGSMITH_PROJECT:-checkout-app}"
export FLAGSMITH_NO_INPUT=1
export XDG_CONFIG_HOME="$RUNTIME/cli-config"   # keep CLI state out of $HOME
unset FS_ADMIN_PASSWORD DJANGO_SECRET_KEY
exec "$RUNTIME/cli/node_modules/.bin/flagsmith" "$@"
