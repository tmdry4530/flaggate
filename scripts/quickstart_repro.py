#!/usr/bin/env python3
"""Runs README quick-start steps 3-5 (keygen, gatekeeper init/start, plan/apply/approve/audit/rollback) against a
Flagsmith runtime, answering the passphrase prompts on a pty with throw-away passphrases. For clean-room
reproduction only: it needs the runtime from scripts/selfhost-up.sh + scripts/seed.sh.

    FLAGSMITH_RUNTIME=<dir> API_PORT=<port> FG_HOME=<empty dir> scripts/quickstart_repro.py [fg-binary]

Prints the real output of each command (passphrases are typed on the pty, not echoed, and not printed).
Exit code != 0 on the first unexpected result.
"""
from __future__ import annotations

import os
import pty
import re
import secrets
import select
import signal
import sys
import time
from pathlib import Path

FG = sys.argv[1] if len(sys.argv) > 1 else "fg"
RUNTIME = os.environ.get("FLAGSMITH_RUNTIME", "../flagsmith-runtime")
API = f"http://127.0.0.1:{os.environ.get('API_PORT', '8600')}"
FG_HOME = os.environ.get("FG_HOME")
if not FG_HOME or any(Path(FG_HOME).expanduser().glob("*")):
    sys.exit("set FG_HOME to an EMPTY directory (clean room: never reuse ~/.flaggate)")
os.environ["FLAGSMITH_API_URL"] = API
APPROVER_PASS, GATEKEEPER_PASS = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def say(line: str) -> None:
    print(line, flush=True)


def run(args: list[str], answers: list[tuple[str, str]] = (), background: bool = False, wait_for: str | None = None):
    """pty-run `args`; whenever the output ends with a prompt regex from `answers` (in order) type the reply."""
    say(f"$ {' '.join(args)}")
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(args[0], args)
    pending, buf, shown = list(answers), "", ""
    deadline = time.time() + 120
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.3)
        if fd in r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            text = ANSI.sub("", data.decode(errors="replace")).replace("\r", "")
            buf += text
            shown += text
            if pending and re.search(pending[0][0] + r"\s*$", buf):
                os.write(fd, (pending[0][1] + "\n").encode())
                pending.pop(0)
                buf = ""
            if background and wait_for and wait_for in shown:
                break
    if background:
        say(shown.rstrip())
        return pid, fd, shown
    _, status = os.waitpid(pid, 0)
    say(shown.rstrip())
    if pending:
        sys.exit(f"unanswered prompts: {[p[0] for p in pending]}")
    return os.waitstatus_to_exitcode(status), shown


def need(cond: bool, what: str) -> None:
    if not cond:
        sys.exit(f"FAILED: {what}")


code, _ = run([FG, "keygen"], [(r"New approver passphrase \(min 8 chars\):", APPROVER_PASS),
                               (r"\(again\):", APPROVER_PASS)])
need(code == 0, "fg keygen")
code, _ = run([FG, "gatekeeper", "init", "--organisation-id", "1", "--project-id", "1", "--environment", "Development",
               "--from-env-file", f"{RUNTIME}/local.env"],
              [(r"New gatekeeper passphrase:", GATEKEEPER_PASS), (r"\(again\):", GATEKEEPER_PASS)])
need(code == 0, "fg gatekeeper init")
gk_pid, gk_fd, _ = run([FG, "gatekeeper", "start"], [], background=True, wait_for="Gatekeeper passphrase")
os.write(gk_fd, (GATEKEEPER_PASS + "\n").encode())
time.sleep(3)
try:
    code, out = run([FG, "plan", "roll out new_checkout to 50% of pro users"])
    need(code == 0, "fg plan")
    pid = re.search(r"^plan\s+(\S+)", out, re.M).group(1)
    need("VERDICT" in out, "no impact verdict in `fg plan`")
    h = re.search(r"^hash\s+([0-9a-f]{64})", out, re.M)
    need(h is not None, "plan hash line")
    say(f"[repro] plan verdict line: {re.search(r'^VERDICT.*$', out, re.M).group(0)}")
    code, out = run([FG, "apply", pid])
    need("REFUSED (no_valid_approval)" in out, "unsigned apply must be refused")
    code, out = run([FG, "approve", pid], [(r"approve \(\w\w\.\.\):", h.group(1)[:4]),
                                           (r"Approver passphrase:", APPROVER_PASS)])
    need(code == 0 and f"APPLIED {pid}" in out, "fg approve must apply the plan")
    code, out = run([FG, "audit", "verify"])
    need(code == 0 and out.startswith("OK") or "chain OK" in out, "fg audit verify")
    code, out = run([FG, "audit", "show"])
    code, out = run([FG, "rollback", pid])
    need(code == 0 and f"ROLLED BACK {pid}" in out, "fg rollback")
    code, out = run([FG, "audit", "verify"])
    need("chain OK" in out, "fg audit verify after rollback")
    say("[repro] quick start steps 3-5 OK")
finally:
    os.kill(gk_pid, signal.SIGINT)
    time.sleep(1)
