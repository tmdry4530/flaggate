#!/usr/bin/env bash
# The one command that re-records and re-renders the whole submission video (no narration, no paid services).
#   demo-video/make-final-video.sh [--edit-only] [--agent planner] [--attempts N] [--out FILE]
# Everything runs in a clean environment: no inherited FS_*/FLAGSMITH_*/FLAGGATE_*/FG_* values, API keys or tokens.
# Needs: scripts/selfhost-up.sh running on :8600 (a throw-away org is created and deleted), demo-video/setup-tools.sh
# done once, Codex CLI logged in with ChatGPT (never with an API key).
set -euo pipefail
cd "$(dirname "$0")/.."
unset GITHUB_TOKEN GH_TOKEN
exec env -i HOME="$HOME" PATH="$PWD/.venv/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \
  TERM=xterm-256color .venv/bin/python demo-video/final/build.py "$@"
