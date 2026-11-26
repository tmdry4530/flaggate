"""DEMO AUTOMATION ONLY. Drives `fg approve` / `fg keygen` through a pseudo-terminal so an unattended demo run
can play the human. It does not weaken anything: `fg approve` still refuses piped stdin, and this script still
has to type the approver passphrase. A real deployment has a human at the keyboard instead of this file."""
from __future__ import annotations

import os
import pty
import re
import select
import time


def run(argv: list[str], script: list[tuple[str, str]], env: dict[str, str], timeout: float = 180) -> tuple[int, str]:
    """Run argv on a pty; for each (regex, reply) wait for regex in the output, then type reply + Enter."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execvpe(argv[0], argv, env)
    buf, out, steps = b"", b"", list(script)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            out += chunk
        if steps and re.search(steps[0][0].encode(), buf):
            time.sleep(0.3)
            os.write(fd, steps[0][1].encode() + b"\r")
            buf = b""
            steps.pop(0)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")
