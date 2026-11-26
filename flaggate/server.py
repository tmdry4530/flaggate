"""Gatekeeper daemon: JSON over HTTP on a unix socket (0600), plus the matching client.

Routes
  GET  /health
  GET  /snapshot                      -> {"snapshot": ..., "state_hash": ...}
  POST /plans            {plan}       -> review bundle (agent path: submit a plan JSON)
  POST /plan-from-prompt {prompt}     -> review bundle (keyless rule-based planner)
  GET  /plans/<id>                    -> review bundle (+ live snapshot for local re-check)
  POST /plans/<id>/apply    {token}   -> apply result      (403 on refusal)
  POST /plans/<id>/rollback {token}   -> rollback result   (403 on refusal)
  GET  /audit/verify                  -> chain check
There is deliberately no route that returns the Flagsmith key or applies without a token.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from flaggate.gatekeeper import Gatekeeper, Refused
from flaggate.plan import Plan
from flaggate.planner import PlannerError
from flaggate.state import state_hash


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(gk: Gatekeeper, lock: threading.Lock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "flaggate-gatekeeper/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet; the audit log is the record
            pass

        def address_string(self) -> str:
            return "unix"

        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _route(self, method: str) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            try:
                with lock:  # one decision at a time: no check/apply races
                    if method == "GET" and parts == ["health"]:
                        return self._send(200, {"ok": True})
                    if method == "GET" and parts == ["snapshot"]:
                        s = gk.snapshot()
                        return self._send(200, {"snapshot": json.loads(s.model_dump_json()), "state_hash": state_hash(s)})
                    if method == "POST" and parts == ["plans"]:
                        b = self._body()
                        plan = Plan.model_validate(b.get("plan", b))
                        return self._send(201, gk.submit(plan, actor=b.get("actor", "agent:mcp")))
                    if method == "POST" and parts == ["plan-from-prompt"]:
                        return self._send(201, gk.plan_from_prompt(self._body()["prompt"]))
                    if method == "GET" and len(parts) == 2 and parts[0] == "plans":
                        return self._send(200, gk.review(parts[1]))
                    if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] in ("apply", "rollback"):
                        token = self._body().get("token")
                        fn = gk.apply if parts[2] == "apply" else gk.rollback
                        return self._send(200, fn(parts[1], token))
                    if method == "GET" and parts == ["audit", "verify"]:
                        return self._send(200, gk.audit_verify())
                return self._send(404, {"error": "not found"})
            except Refused as e:
                return self._send(403, {"refused": True, "reason": e.reason, "detail": e.detail})
            except (ValidationError, PlannerError, ValueError, KeyError) as e:
                return self._send(400, {"error": type(e).__name__, "detail": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": type(e).__name__, "detail": str(e)})

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

    return Handler


def serve(gk: Gatekeeper, sock_path: Path) -> _UnixHTTPServer:
    sock_path = Path(sock_path)
    if sock_path.exists():
        sock_path.unlink()
    old = os.umask(0o177)
    try:
        srv = _UnixHTTPServer(str(sock_path), make_handler(gk, threading.Lock()))
    finally:
        os.umask(old)
    os.chmod(sock_path, 0o600)
    return srv


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

    def submit(self, plan: dict[str, Any], actor: str = "agent:mcp") -> dict[str, Any]:
        return self._call("POST", "/plans", {"plan": plan, "actor": actor})

    def plan_from_prompt(self, prompt: str) -> dict[str, Any]:
        return self._call("POST", "/plan-from-prompt", {"prompt": prompt})

    def review(self, plan_id: str) -> dict[str, Any]:
        return self._call("GET", f"/plans/{plan_id}")

    def apply(self, plan_id: str, token: str | None) -> dict[str, Any]:
        return self._call("POST", f"/plans/{plan_id}/apply", {"token": token})

    def rollback(self, plan_id: str, token: str | None) -> dict[str, Any]:
        return self._call("POST", f"/plans/{plan_id}/rollback", {"token": token})

    def audit_verify(self) -> dict[str, Any]:
        return self._call("GET", "/audit/verify")
