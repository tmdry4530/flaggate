#!/usr/bin/env bash
# Clean-room reproduction of the README quick start: fresh clone of this repo, EMPTY Flagsmith runtime, other ports,
# own FG_HOME, own uv cache, nothing from an existing runtime in the environment. Prints wall time and disk per step.
#
#   scripts/cleanroom-repro.sh [cleanroom-dir] [log-dir]
#     cleanroom-dir  default $TMPDIR/cleanroom-flagsmith (repo clone, runtime, FG_HOME, uv cache; removed by --clean)
#     log-dir        default <cleanroom-dir>-logs             (kept)
#   API_PORT (default 8601), PG_PORT (default 55433)
#   scripts/cleanroom-repro.sh --clean [cleanroom-dir]       stop THAT runtime (its own pid file/data dir) and delete it
set -uo pipefail
if [ "${1:-}" = "--clean" ]; then
  CR="${2:-${TMPDIR:-/tmp}/cleanroom-flagsmith}"
  case "$CR" in */flagsmith-runtime|"") echo "refusing: $CR" >&2; exit 2;; esac
  for p in $(pgrep -f "$CR/repo/.venv/bin/fg gatekeeper start" || true); do kill "$p"; done
  [ -f "$CR/rt/api.pid" ] && kill "$(cat "$CR/rt/api.pid")" 2>/dev/null
  [ -x "$CR/rt/pgconda/bin/pg_ctl" ] && "$CR/rt/pgconda/bin/pg_ctl" -D "$CR/rt/pgdata" stop -m fast | tail -1
  sleep 2; rm -rf "$CR"; echo "removed $CR"; exit 0
fi
unset GITHUB_TOKEN GH_TOKEN UV_PROJECT_ENVIRONMENT
for v in $(env | grep -o '^FS_[A-Z_]*\|^FLAGGATE[A-Z_]*\|^FLAGSMITH[A-Z_]*\|^FG_[A-Z_]*'); do unset "$v"; done
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CR="${1:-${TMPDIR:-/tmp}/cleanroom-flagsmith}"; LOGS="${2:-$CR-logs}"
export FLAGSMITH_RUNTIME="$CR/rt" API_PORT="${API_PORT:-8601}" PG_PORT="${PG_PORT:-55433}" FLAGSMITH_LOGDIR="$LOGS" \
       FG_HOME="$CR/fghome" UV_CACHE_DIR="$CR/uvcache" FLAGSMITH_API_URL="http://127.0.0.1:${API_PORT:-8601}"
[ -e "$CR" ] && { echo "$CR exists; run --clean first" >&2; exit 2; }
ss -ltn | grep -qE ":($API_PORT|$PG_PORT)\b" && { echo "port $API_PORT or $PG_PORT is in use" >&2; exit 2; }
avail=$(df -BG --output=avail "$(dirname "$CR")" | tail -1 | tr -dc 0-9); [ "$avail" -ge 8 ] || { echo "free disk ${avail}G < 8G" >&2; exit 1; }
mkdir -p "$CR" "$LOGS" "$FG_HOME"
T0=$(date +%s)
mark() { echo "$(( $(date +%s) - T0 ))s  $*  | disk used by clean room: $(du -sm "$CR" | cut -f1) MB" | tee -a "$LOGS/timing.txt"; }
: > "$LOGS/timing.txt"; mark start
git clone -q "$SRC" "$CR/repo" && cd "$CR/repo" && mark "git clone"
uv sync --extra dev > "$LOGS/step1-uv-sync.log" 2>&1; mark "step 1 uv sync (rc=$?)"
export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/pytest -q > "$LOGS/step1-pytest-no-server.log" 2>&1; mark "step 1 pytest, no server: $(tail -1 "$LOGS/step1-pytest-no-server.log")"
scripts/selfhost-up.sh > "$LOGS/step2-selfhost-up.log" 2>&1; mark "step 2 selfhost-up (rc=$?)"
scripts/seed.sh > "$LOGS/step2-seed.out" 2>&1; mark "step 2 seed (rc=$?)"
scripts/quickstart_repro.py > "$LOGS/step3-5-quickstart.log" 2>&1; mark "steps 3-5 keygen..rollback (rc=$?)"
.venv/bin/pytest -q > "$LOGS/step6-pytest-live.log" 2>&1; mark "pytest against the clean-room server: $(tail -1 "$LOGS/step6-pytest-live.log")"
mark done
