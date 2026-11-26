#!/usr/bin/env bash
# Self-host Flagsmith API (open-source Django app) on a box with NO docker and NO sudo.
#   - Flagsmith source:  shallow sparse clone of github.com/Flagsmith/flagsmith (api/ only)
#   - Python deps:       uv sync --frozen --no-dev (Python 3.13) into $RUNTIME/venv
#   - Postgres server:   PostgreSQL 16 from conda-forge, installed with a static micromamba ($RUNTIME/pgconda)
#                        run as the current user, data in $RUNTIME/pgdata, 127.0.0.1:$PG_PORT
#   - API:               gunicorn via `flagsmith start api`-equivalent, settings app.settings.production
# Everything lives OUTSIDE this git repo (in $RUNTIME). Secrets go to $RUNTIME/local.env (chmod 600), never printed.
# Idempotent: re-running skips finished steps.
set -euo pipefail
unset GITHUB_TOKEN GH_TOKEN || true

FLAGSMITH_VERSION="${FLAGSMITH_VERSION:-v2.295.2}"
RUNTIME="${FLAGSMITH_RUNTIME:-$(cd "$(dirname "$0")/../.." && pwd)/flagsmith-runtime}"  # default: sibling of the repo
PG_PORT="${PG_PORT:-55432}"
API_PORT="${API_PORT:-8600}"
LOGDIR="${FLAGSMITH_LOGDIR:-$RUNTIME/logs}"
NICE="nice -n 19 ionice -c3"

mkdir -p "$RUNTIME" "$LOGDIR"
cd "$RUNTIME"

# --- 0. disk guard -----------------------------------------------------------
avail_gb=$(df -BG --output=avail "$RUNTIME" | tail -1 | tr -dc 0-9)
if [ "$avail_gb" -lt 8 ]; then echo "free disk ${avail_gb}G < 8G, abort" >&2; exit 1; fi

# --- 1. source ---------------------------------------------------------------
if [ ! -d src/api ]; then
  $NICE git clone --depth 1 --branch "$FLAGSMITH_VERSION" --filter=blob:none --sparse \
    https://github.com/Flagsmith/flagsmith.git src
  git -C src sparse-checkout set api
fi

# --- 2. python deps (API) ----------------------------------------------------
export UV_PROJECT_ENVIRONMENT="$RUNTIME/venv"
if [ ! -x venv/bin/python ] || [ ! -f venv/.synced ]; then
  (cd src/api && $NICE uv sync --frozen --no-dev --python 3.13) >"$LOGDIR/uv-sync.log" 2>&1
  touch venv/.synced
fi

# --- 3. postgres server binaries (user-space, conda-forge via micromamba, no sudo) ---
# NOTE: the `pgserver` PyPI wheel (PG 16.2) was tried first but lacks contrib extensions;
# Flagsmith migrations need pg_trgm, so we use conda-forge `postgresql=16` (includes contrib).
PGBIN="$RUNTIME/pgconda/bin"
if [ ! -x "$PGBIN/postgres" ]; then
  mkdir -p tools
  if [ ! -x tools/bin/micromamba ]; then
    curl -fsSL -o tools/mm.tar.bz2 https://micro.mamba.pm/api/micromamba/linux-64/latest
    python3 -c "import tarfile;tarfile.open('tools/mm.tar.bz2').extract('bin/micromamba','tools',filter='data')"
    rm -f tools/mm.tar.bz2
  fi
  MAMBA_ROOT_PREFIX="$RUNTIME/mamba" $NICE tools/bin/micromamba create -y -q -p "$RUNTIME/pgconda" \
    -c conda-forge --override-channels postgresql=16 >"$LOGDIR/micromamba.log" 2>&1
  MAMBA_ROOT_PREFIX="$RUNTIME/mamba" tools/bin/micromamba clean -a -y -q >/dev/null 2>&1 || true
fi

# --- 4. secrets (generated locally, never printed) ---------------------------
if [ ! -f local.env ]; then
  umask 077
  cat > local.env <<EOS
DJANGO_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
FS_ADMIN_EMAIL=${FLAGSMITH_ADMIN_EMAIL:-admin@localhost.test}
FS_ADMIN_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
EOS
  umask 022
fi
set -a; . ./local.env; set +a

# --- 5. postgres cluster -----------------------------------------------------
mkdir -p pgsock
if [ ! -f pgdata/PG_VERSION ]; then
  "$PGBIN/initdb" -D pgdata -U postgres --auth=trust -E UTF8 --locale=C.UTF-8 >"$LOGDIR/initdb.log" 2>&1
fi
if ! "$PGBIN/pg_ctl" -D pgdata status >/dev/null 2>&1; then
  "$PGBIN/pg_ctl" -D pgdata -l "$LOGDIR/postgres.log" -w \
    -o "-p $PG_PORT -k $RUNTIME/pgsock -c listen_addresses=127.0.0.1 -c shared_buffers=64MB -c max_connections=50" start
fi
"$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U postgres -tAc "select 1 from pg_database where datname='flagsmith'" | grep -q 1 \
  || "$PGBIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U postgres flagsmith

# --- 6. django env -----------------------------------------------------------
export DATABASE_URL="postgresql://postgres@127.0.0.1:$PG_PORT/flagsmith"
export DJANGO_SETTINGS_MODULE=app.settings.production
export DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"
export PGGSSENCMODE=disable
export ENABLE_ADMIN_ACCESS_USER_PASS=true
export PREVENT_SIGNUP=true          # nobody can self-register on this instance
export TASK_RUN_METHOD=SYNCHRONOUSLY
export ENVIRONMENT_CACHE_SECONDS=0
PY="$RUNTIME/venv/bin/python"
cd "$RUNTIME/src/api"

if [ ! -f "$RUNTIME/.migrated-$FLAGSMITH_VERSION" ]; then
  $NICE "$PY" manage.py migrate --noinput >"$LOGDIR/migrate.log" 2>&1
  "$PY" manage.py createcachetable >>"$LOGDIR/migrate.log" 2>&1
  touch "$RUNTIME/.migrated-$FLAGSMITH_VERSION"
fi

# superuser (idempotent)
"$PY" manage.py shell -c "
import os
from django.contrib.auth import get_user_model
U=get_user_model(); e=os.environ['FS_ADMIN_EMAIL']
if not U.objects.filter(email=e).exists():
    U.objects.create_superuser(email=e, password=os.environ['FS_ADMIN_PASSWORD'], first_name='Local', last_name='Admin')
    print('superuser created')
else:
    print('superuser exists')
" >>"$LOGDIR/migrate.log" 2>&1

# --- 7. API server -----------------------------------------------------------
if [ -f "$RUNTIME/api.pid" ] && kill -0 "$(cat "$RUNTIME/api.pid")" 2>/dev/null; then
  echo "API already running pid=$(cat "$RUNTIME/api.pid") port=$API_PORT"
else
  nohup "$RUNTIME/venv/bin/gunicorn" app.wsgi:application --bind "127.0.0.1:$API_PORT" \
    --workers 2 --timeout 60 --access-logfile "$LOGDIR/api-access.log" \
    >"$LOGDIR/api.log" 2>&1 &
  echo $! > "$RUNTIME/api.pid"
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$API_PORT/health/liveness/" >/dev/null 2>&1 && break
    sleep 1
  done
fi
curl -fsS "http://127.0.0.1:$API_PORT/version/" ; echo
echo "flagsmith api: http://127.0.0.1:$API_PORT  pid=$(cat "$RUNTIME/api.pid")  pg=127.0.0.1:$PG_PORT  logs=$LOGDIR"
