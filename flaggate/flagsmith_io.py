"""Minimal Flagsmith HTTP access for the impact engine (stdlib only) + local credential loading.

Credentials are read from the process environment first, then from a ``KEY=VALUE`` env file
(``FLAGGATE_ENV_FILE``, else ``$FLAGSMITH_RUNTIME/local.env`` of the local self-host spike). The values are
never logged, printed, or placed in exceptions: errors mention the URL path and HTTP status only.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .identities import Identity

DEFAULT_API_URL = "http://127.0.0.1:8600"
DEFAULT_RUNTIME = "../flagsmith-runtime"


class FlagsmithHTTPError(RuntimeError):
    pass


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_credentials(extra_file: str | os.PathLike | None = None) -> dict[str, str]:
    """Return FS_* values (FS_ENV_KEY, FS_SERVER_KEY, FS_ADMIN_API_KEY, ...) found in env or env file."""
    path = Path(extra_file or os.environ.get("FLAGGATE_ENV_FILE")
                or Path(os.environ.get("FLAGSMITH_RUNTIME", DEFAULT_RUNTIME)) / "local.env")
    merged = _read_env_file(path)
    merged.update({k: v for k, v in os.environ.items() if k.startswith("FS_")})
    return merged


def api_url_from_env() -> str:
    return os.environ.get("FLAGSMITH_API_URL", DEFAULT_API_URL).rstrip("/")


def _request(api_url: str, path: str, headers: dict[str, str], timeout: float = 20.0) -> Any:
    req = urllib.request.Request(api_url.rstrip("/") + path, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise FlagsmithHTTPError(f"GET {path.split('?')[0]} -> HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FlagsmithHTTPError(f"GET {path.split('?')[0]} failed: {type(exc).__name__}") from None


def fetch_environment_document(api_url: str, server_key: str) -> dict[str, Any]:
    """GET /api/v1/environment-document/ with a server-side key. The document carries the env client key
    as ``api_key``; callers must not log the document."""
    return _request(api_url, "/api/v1/environment-document/", {"X-Environment-Key": server_key})


def server_reachable(api_url: str = DEFAULT_API_URL, timeout: float = 2.0) -> bool:
    try:
        _request(api_url, "/health/liveness/", {}, timeout=timeout)
        return True
    except FlagsmithHTTPError:
        return False


def fetch_identities(api_url: str, admin_api_key: str, env_key: str, *, limit: int | None = 500,
                     page_size: int = 100) -> list[Identity]:
    """Identities + traits through the Admin API (needs an Api-Key; traits are one call per identity)."""
    headers = {"Authorization": f"Api-Key {admin_api_key}"}
    base = f"/api/v1/environments/{urllib.parse.quote(env_key)}/identities/"
    rows: list[dict[str, Any]] = []
    page = 1
    while limit is None or len(rows) < limit:
        data = _request(api_url, f"{base}?page={page}&page_size={page_size}", headers)
        rows.extend(data["results"])
        if not data.get("next"):
            break
        page += 1
    if limit is not None:
        rows = rows[:limit]

    def with_traits(row: dict[str, Any]) -> Identity:
        traits = _request(api_url, f"{base}{row['id']}/traits/?page_size=100", headers)["results"]
        return Identity(row["identifier"], {t["trait_key"]: _trait_value(t) for t in traits})

    # one traits call per identity: run them concurrently (order is preserved by map)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(with_traits, rows))


def _trait_value(t: dict[str, Any]) -> Any:
    for key in ("string_value", "integer_value", "float_value", "boolean_value"):
        if t.get(key) is not None:
            return t[key]
    return None
