# Self-hosting Flagsmith on the box (no docker, no sudo, no account)

Verified 2026-11-26 (KST) against **Flagsmith v2.295.2** (git tag; `api/pyproject.toml` says `flagsmith-api 2.68.0`).

## TL;DR

```bash
scripts/selfhost-up.sh     # source + deps + user-space Postgres 16 + migrate + superuser + gunicorn on 127.0.0.1:8600
scripts/seed.sh            # Admin API: org/project/env/flags/MV/segment/override + official Python SDK read-back
scripts/fs-cli.sh flag list            # official Flagsmith CLI v2 against the local instance
$FLAGSMITH_RUNTIME/mcpvenv/bin/python scripts/mcp-probe.py   # official Flagsmith MCP server (stdio)
scripts/selfhost-down.sh   # stops only what selfhost-up started (pid file / pg_ctl)
```

Runtime lives **outside** the repo: `../flagsmith-runtime/`
(`src/` sparse clone, `venv/` API deps, `pgconda/` Postgres, `pgdata/`, `mcpvenv/`, `cli/`, `logs/`,
`local.env` = generated secrets, chmod 600, never committed/printed). Disk used ≈ 630 MB (plus uv/npm caches in $HOME).

## What works / what doesn't

| Method | Result | Evidence |
|---|---|---|
| Docker / docker compose (official path) | ✗ not installed on the box; not attempted | `which docker podman` → nothing |
| Rootless podman | ✗ not attempted: needs `newuidmap`/subuid setup + distro packages (sudo) | — |
| Flagsmith API from source with `uv sync --frozen --no-dev` (Python 3.13) | ✓ | `logs/uv-sync.log` |
| SQLite | – not tried: migrations create the Postgres extension `pg_trgm` (TrigramExtension), so SQLite is not expected to work | grep in `src/api` |
| Postgres from `pgserver` PyPI wheel (PG 16.2) | ✗ wheel has no contrib → `extension "pg_trgm" is not available` during migrate | first `migrate` run |
| `postgresql-wheel` PyPI | ✗ no cp313 wheel (only cp37–cp39) | uv resolver error |
| `apt-get download postgresql-17` | ✗ no candidate (apt lists empty, no root for `apt update`) | — |
| **Postgres 16.16 from conda-forge via static `micromamba`** | ✓ includes contrib (`pg_trgm`) | `logs/micromamba.log`, `logs/postgres.log` |
| `manage.py migrate` + `createcachetable` (settings `app.settings.production`) | ✓ | `logs/migrate.log` |
| gunicorn `app.wsgi` on 127.0.0.1:8600 | ✓ `/health/liveness/` 200, `/version/` 200 | `logs/api.log`, `logs/api-access.log` |
| Admin API with org **Admin API key** (`Authorization: Api-Key …`) | ✓ create project/env/flags/MV/segment/segment-override, update env feature state | `logs/seed.log` |
| Official **Python SDK** `flagsmith==6.3.0` (remote eval + local eval with `ser.` key) | ✓ | `logs/seed.log` |
| Official **Flagsmith CLI** `@flagsmith/cli` v2.0.0 (npm, Go binary) with `FLAGSMITH_API_URL` + host-scoped key `FLAGSMITH_API_KEY_127_0_0_1_8600` | ✓ read **and** write (`flag enable/disable/update --value/--weight`, `evaluate --identity` shows `TARGETING_MATCH; segment=pro_users`, `SPLIT; weight=33.0`) | `logs/cli-test.log` |
| Official **Flagsmith MCP server** (repo `mcp/` subdir, fastmcp 3.4.11) over **stdio**, `FLAGSMITH_API_URL=http://127.0.0.1:8600`, `FLAGSMITH_API_TOKEN=<admin api key>` | ✓ 54 tools; `list_organizations`, `list_project_features`, `list_project_segments`, `create_feature` (write) all OK | `logs/mcp-probe.log` |
| Native **change requests / 4-eyes approval** (`minimum_change_request_approvals`) | ✗ on OSS/free plan: field is read-only (PATCH ignored, stays `null`), `GET …/list-change-requests/` → 404; logic lives in private `workflows_logic` package | manual curl, `environments/serializers.py: ReadOnlyIfNotValidPlanMixin invalid_plans=("free",)` |
| Audit log API `GET /api/v1/audit/?project=1` | ✗ with `Api-Key` → **500** (`TypeError … APIKeyUser`, upstream bug); with user Token → `count: 0` although 24 `AuditLog` rows exist, because free plan `audit_log_visibility_days = 0` | `logs/api.log` |
| Experiments API (`/experiments/`, `/experiment-metrics/`) | ✗ `experimentation_enabled: false` in `/version/` on this deployment | `/version/` |

→ On self-hosted OSS there is **no human approval gate and no visible audit trail**. That gap is what our agent fills (see DESIGN.md).

## Gotchas
- CLI needs `--yes` (or a TTY) for writes; variants without a `key` must be addressed by **id** (`--weight 1=50,2=40`), `--weight green=50` fails.
- CLI only reads **host-scoped** credentials for self-hosted: `FLAGSMITH_API_KEY_<host>_<port>` (`.`/`:` → `_`, `-` → `--`).
- MCP stdio has no OAuth; it must be given a static Admin API key → any agent holding that MCP config can write directly. Our design keeps that key away from the agent.
- `PREVENT_SIGNUP=true` is set: nobody can self-register on the local instance.
