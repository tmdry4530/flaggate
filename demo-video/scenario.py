#!/usr/bin/env python3
"""The part that is recorded: runs INSIDE the asciinema pty, started by record.py.

It "types" each command like a person (so the cast shows the command line), runs the REAL `fg` command, and for
`fg approve` (which insists on a TTY and a hidden passphrase) drives a child pty: it types the 4-character hash
prefix and the throw-away approver passphrase (FG_DEMO_APPROVER_PASS, never echoed: getpass turns echo off).
Everything printed is real output of the real command. Nothing here talks to Flagsmith directly.

    FG_HOME / FG_SOCKET / FG_DEMO_APPROVER_PASS come from record.py; SCENARIO selects the script below.
"""
from __future__ import annotations

import os
import pty
import re
import select
import sys
import time

FG = os.environ.get("FG_BIN", "fg")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROMPT = "\x1b[1;32m$\x1b[0m "


def out(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def typed(cmd: str, cps: float = 55.0) -> None:
    out(PROMPT)
    for ch in cmd:
        out(ch)
        time.sleep(1 / cps)
    out("\n")
    time.sleep(0.25)


def comment(text: str, pause: float = 0.8) -> None:
    out(f"\x1b[2m# {text}\x1b[0m\n")
    time.sleep(pause)


def run_capture(args: list[str]) -> tuple[str, int]:
    """Run a command on a pty (so isatty() is true for the child, as in a real terminal), relay its bytes to our
    stdout (= the recorded terminal) and return (plain text output, exit code)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(args[0], args)
    full = ""
    while True:
        r, _, _ = select.select([fd], [], [], 0.2)
        if fd in r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            text = data.decode(errors="replace")
            out(text)
            full += text
    _, status = os.waitpid(pid, 0)
    return ANSI.sub("", full), os.waitstatus_to_exitcode(status)


def shell(cmd: str, args: list[str]) -> tuple[str, int]:
    typed(cmd)
    return run_capture(args)


def plan_id_from(text: str) -> str:
    m = re.search(r"^plan\s+(\S+)", text, re.M)
    if not m:
        raise SystemExit("could not find the plan id in `fg plan` output")
    return m.group(1)


def clear(pause: float = 1.4) -> None:
    time.sleep(pause)
    out("\x1b[2J\x1b[H")


def test_clip() -> None:
    """~20 s: plan + impact preview -> agent cannot apply -> human approves -> applied and verified."""
    prompt = "roll out new_checkout to 25% of pro users"
    comment("an agent asks FlagGate for a change; FlagGate shows who would be affected before anything happens")
    text, _ = shell(f'fg plan "{prompt}"', [FG, "plan", prompt])
    pid = plan_id_from(text)
    clear(2.5)
    comment("the agent has no Flagsmith key and no approval: it cannot apply")
    shell(f"fg apply {pid}", [FG, "apply", pid])
    time.sleep(1.2)
    clear(0.2)
    comment("the human re-checks the plan, types the hash prefix and the passphrase")
    typed(f"fg approve {pid}")
    approve(pid)
    time.sleep(1.5)


def approve(pid: str) -> tuple[str, int]:
    """Drive `fg approve`: read the hash prefix off the screen like a human would, then the passphrase."""
    pidc, fd = pty.fork()
    if pidc == 0:
        os.execvp(FG, [FG, "approve", pid])
    seen = ""
    full = ""
    stage = 0
    while True:
        r, _, _ = select.select([fd], [], [], 0.2)
        if fd not in r:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        text = data.decode(errors="replace")
        out(text)
        seen += text
        full += text
        plain = ANSI.sub("", seen)
        if stage == 0 and "Type the first 4 characters of the plan hash" in plain:
            h = re.search(r"^hash\s+([0-9a-f]{64})", ANSI.sub("", full), re.M).group(1)
            time.sleep(1.2)
            for ch in h[:4]:
                os.write(fd, ch.encode())
                time.sleep(0.12)
            os.write(fd, b"\n")
            stage, seen = 1, ""
        elif stage == 1 and "Approver passphrase" in plain:
            time.sleep(0.8)
            os.write(fd, (os.environ["FG_DEMO_APPROVER_PASS"] + "\n").encode())  # not echoed: getpass
            stage, seen = 2, ""
    _, status = os.waitpid(pidc, 0)
    return ANSI.sub("", full), os.waitstatus_to_exitcode(status)


def readme_demo() -> None:
    """~30 s: the README "30-second demo": rollout + A/B plan, refused apply, human approval, rollback, audit check."""
    prompt = ("roll out new_checkout to 25% of pro users; "
              "a/b test checkout_button_color green vs orange 50/50")
    text, _ = shell(f'fg plan "{prompt}"', [FG, "plan", prompt])
    pid = plan_id_from(text)
    time.sleep(1.5)
    shell(f"fg apply {pid}", [FG, "apply", pid])
    time.sleep(1.0)
    typed(f"fg approve {pid}")
    approve(pid)
    time.sleep(1.2)
    shell(f"fg rollback {pid}", [FG, "rollback", pid])
    time.sleep(0.8)
    shell("fg audit verify", [FG, "audit", "verify"])
    time.sleep(0.8)
    shell("fg audit show", [FG, "audit", "show"])
    time.sleep(1.5)


SCENARIOS = {"test-clip": test_clip, "readme-demo": readme_demo}

if __name__ == "__main__":
    name = os.environ.get("SCENARIO", "test-clip")
    out("\x1b[2J\x1b[H")
    SCENARIOS[name]()
