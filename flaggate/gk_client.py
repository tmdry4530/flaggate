"""Client for the gatekeeper's unix-socket API. Standard library only: importing this module pulls in no
Flagsmith client, sealed secrets or key-handling code (fg-mcp depends on that)."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 120):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


class GatekeeperError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(body.get("detail") or body.get("error") or str(body))
        self.status, self.body = status, body


class GatekeeperClient:
    def __init__(self, sock_path: Path):
        self.sock_path = str(sock_path)

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        conn = _UnixConn(self.sock_path)
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            out = json.loads(r.read() or b"{}")
        finally:
            conn.close()
        if r.status >= 300:
            raise GatekeeperError(r.status, out)
        return out

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def snapshot(self) -> dict[str, Any]:
        return self._call("GET", "/snapshot")

    def submit(self, plan: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/plans", {"plan": plan, "actor": actor})

    def plan_from_prompt(self, prompt: str, actor: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/plan-from-prompt", {"prompt": prompt, "actor": actor})

    def status(self, plan_id: str) -> dict[str, Any]:
        return self._call("GET", f"/plans/{plan_id}/status")

    def review(self, plan_id: str) -> dict[str, Any]:
        return self._call("GET", f"/plans/{plan_id}")

    def apply(self, plan_id: str, token: str | None) -> dict[str, Any]:
        return self._call("POST", f"/plans/{plan_id}/apply", {"token": token})

    def rollback(self, plan_id: str, token: str | None) -> dict[str, Any]:
        return self._call("POST", f"/plans/{plan_id}/rollback", {"token": token})

    def audit_verify(self) -> dict[str, Any]:
        return self._call("GET", "/audit/verify")
