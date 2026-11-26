"""Gatekeeper daemon: JSON over HTTP on a unix socket (0600), plus the matching client.

Routes
  GET  /health
  GET  /snapshot                      -> {"snapshot": ..., "state_hash": ...}
  POST /plans            {plan}       -> review bundle (agent path: submit a plan JSON)
  POST /plan-from-prompt {prompt}     -> review bundle (keyless rule-based planner)
  GET  /plans/<id>                    -> review bundle (+ live snapshot for local re-check)
  POST /plans/<id>/apply    {token}   -> apply result      (403 on refusal)
  POST /plans/<id>/rollback {token}   -> rollback result   (403 on refusal)
  GET  /plans/<id>/status             -> cheap status (+ terminal result)
  GET  /audit/verify                  -> chain check
There is deliberately no route that returns the Flagsmith key or applies without a token.

Two sockets, same daemon: ``gk.sock`` (role "human": everything above) and ``agent.sock`` (role "agent": no
apply/rollback routes at all; this is the only socket fg-mcp is given). Both are 0600.
"""

from __future__ import annotations

import json
import os
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


AGENT_ROLE, HUMAN_ROLE = "agent", "human"


def make_handler(gk: Gatekeeper, lock: threading.Lock, role: str = HUMAN_ROLE) -> type[BaseHTTPRequestHandler]:
    if role not in (AGENT_ROLE, HUMAN_ROLE):
        raise ValueError(f"unknown socket role {role!r}")

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
            if role == AGENT_ROLE and len(parts) == 3 and parts[0] == "plans" and parts[2] not in ("status",):
                gk.audit.append("refused", plan_id=parts[1], action=parts[2], reason="agent_socket",
                                detail="apply/rollback are not available on the agent socket")
                return self._send(404, {"error": "not found", "detail": "not available on the agent socket"})
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
                        return self._send(201, gk.submit(plan, actor=b.get("actor") or ("agent:mcp" if role == AGENT_ROLE else "human:cli")))
                    if method == "POST" and parts == ["plan-from-prompt"]:
                        b = self._body()
                        actor = b.get("actor") or ("agent:mcp" if role == AGENT_ROLE else "human:cli")
                        return self._send(201, gk.plan_from_prompt(b["prompt"], actor=actor))
                    if method == "GET" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "status":
                        return self._send(200, gk.status(parts[1]))
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


def serve(gk: Gatekeeper, sock_path: Path, role: str = HUMAN_ROLE,
          lock: threading.Lock | None = None) -> _UnixHTTPServer:
    sock_path = Path(sock_path)
    if sock_path.exists():
        sock_path.unlink()
    old = os.umask(0o177)
    try:
        srv = _UnixHTTPServer(str(sock_path), make_handler(gk, lock or threading.Lock(), role))
    finally:
        os.umask(old)
    os.chmod(sock_path, 0o600)
    return srv


# client side lives in flaggate.gk_client (importable without the gatekeeper/Flagsmith code); re-exported here
from flaggate.gk_client import GatekeeperClient, GatekeeperError  # noqa: E402,F401
