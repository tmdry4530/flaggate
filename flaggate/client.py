"""Minimal Flagsmith Admin (Management) API client for a self-hosted instance.

Stdlib only. Auth: ``Authorization: Api-Key <organisation admin API key>``.
Every call is recorded in ``self.calls`` (method, path, status) for the audit log.
Secrets are never included in recorded paths: environment keys are replaced by
``<env>`` via :meth:`redact`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class FlagsmithError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.method, self.path, self.status, self.body = method, path, status, body


@dataclass
class FlagsmithClient:
    api_url: str
    api_key: str = field(repr=False)
    timeout: float = 30.0
    secrets: tuple[str, ...] = field(default=(), repr=False)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def redact(self, text: str) -> str:
        for s in (self.api_key, *self.secrets):
            if s:
                text = text.replace(s, "<env>" if s != self.api_key else "<key>")
        return text

    def request(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        url = self.api_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Api-Key {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        shown = self.redact(path)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                self.calls.append({"method": method, "path": shown, "status": r.status})
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            self.calls.append({"method": method, "path": shown, "status": e.code})
            raise FlagsmithError(method, shown, e.code, self.redact(text)) from None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body)

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def list_all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Follow DRF pagination (page / page_size)."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get(path, page=page, page_size=100, **params)
            if isinstance(data, list):
                return data
            out.extend(data.get("results", []))
            if not data.get("next"):
                return out
            page += 1

    # --- convenience lookups -------------------------------------------------
    def environment(self, env_key: str) -> dict[str, Any]:
        return self.get(f"/api/v1/environments/{env_key}/")

    def ping(self) -> bool:
        try:
            self.get("/api/v1/organisations/")
            return True
        except Exception:
            return False
