"""A real interactive bash on a pty, typed into like a person, relayed to our stdout (= the asciinema pty).

Everything that appears on screen is the real output of real commands. Two cosmetic layers sit on the relay only:
  * sanitize(): local paths are rewritten before they reach the recording (screen hygiene),
  * marker(): invisible OSC strings carrying a name and a wall-clock epoch; the editor uses them to cut, align
    and subtitle the recording (they are removed before rendering).
"""
from __future__ import annotations

import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import fcntl
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
PATH_RULES = [
    (re.compile(r"/tmp/fg-[A-Za-z0-9_]+(?:/work)?"), "~/demo"),
    (re.compile(r"/tmp/fg-[A-Za-z0-9_]+/h"), "~/.flaggate"),
    # machine-specific roots, computed at runtime so no host path is hard-coded
    (re.compile(re.escape(str(Path.home())) + r"\b/?\S*"), "~"),
    (re.compile(re.escape("/" + Path(__file__).resolve().parts[1]) + r"/\S*"), "~/flagsmith"),
]
PS1 = r"\[\e[1;32m\]$\[\e[0m\] "


def sanitize(text: str) -> str:
    for rx, rep in PATH_RULES:
        text = rx.sub(rep, text)
    return text


def out(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def marker(name: str) -> None:
    """Invisible marker in the recording: OSC 7777 ; <name> ; <epoch> BEL."""
    out(f"\x1b]7777;{name};{time.time():.3f}\x07")


class Typist:
    def __init__(self, env: dict[str, str], cwd: str, cols: int, rows: int, typing_cps: float = 22.0,
                 rc_extra: str = ""):
        self.cps = typing_cps
        self.before = ""         # text printed before the last expect() match
        self.buf = ""            # unsanitised text not yet relayed (partial line)
        self.seen = ""           # everything since the last expect() match, ANSI-stripped
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            rc = Path(cwd) / f".bashrc-demo-{os.getpid()}"       # one rc per shell: the two terminals share this directory
            rc.write_text(f"PS1='{PS1}'\nPROMPT_COMMAND=''\nPS2='> '\nenable -n fg bg 2>/dev/null\nbind 'set enable-bracketed-paste off' 2>/dev/null\n{rc_extra}\n")
            os.execvpe("bash", ["bash", "--noprofile", "--rcfile", str(rc), "-i"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    # -- reading
    def pump(self, timeout: float = 0.15) -> bool:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if self.fd in r:
            try:
                data = os.read(self.fd, 8192)
            except OSError:
                return False
            if not data:
                return False
            text = data.decode(errors="replace")
            self.seen += ANSI.sub("", text)
            self.buf += text
            cut = self.buf.rfind("\n")
            if cut >= 0:
                out(sanitize(self.buf[: cut + 1]))
                self.buf = self.buf[cut + 1:]
        elif self.buf:                      # prompt or partial line after a quiet moment
            out(sanitize(self.buf))
            self.buf = ""
        return True

    def expect(self, regex: str, timeout: float = 120.0) -> re.Match:
        rx, deadline = re.compile(regex), time.time() + timeout
        while time.time() < deadline:
            m = rx.search(self.seen)
            if m:
                self.before = self.seen[: m.start()]
                self.seen = self.seen[m.end():]
                return m
            if not self.pump():
                break
        raise TimeoutError(f"timed out waiting for /{regex}/")

    def settle(self, quiet: float = 0.6) -> None:
        """Relay output until nothing arrives for `quiet` seconds."""
        last = time.time()
        while time.time() - last < quiet:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if r:
                if not self.pump(0):
                    return
                last = time.time()
            elif self.buf:
                self.pump(0)

    # -- writing
    def send(self, s: str) -> None:
        os.write(self.fd, s.encode())

    def type(self, text: str, cps: float | None = None, enter: bool = True) -> None:
        for ch in text:
            self.send(ch)
            self.settle_char(1 / (cps or self.cps))
        if enter:
            self.settle_char(0.35)
            self.send("\n")
            self.seen = ""

    def settle_char(self, dt: float) -> None:
        end = time.time() + dt
        while time.time() < end:
            self.pump(min(0.05, max(0.0, end - time.time())))

    def run(self, cmd: str, wait_prompt: bool = True, timeout: float = 180.0, cps: float | None = None) -> str:
        """Type a command, wait for the next prompt and return what the command printed (ANSI stripped)."""
        self.type(cmd, cps)
        if wait_prompt:
            self.wait_prompt(timeout)
        return self.before

    def wait_prompt(self, timeout: float = 180.0) -> None:
        """Wait for the next bash prompt (a line that is just '$ ')."""
        self.expect(r"(?:^|\n)\$ $", timeout)

    def quiet(self, seconds: float) -> None:
        """Hold the screen (real waiting) for `seconds` while still relaying anything that arrives."""
        end = time.time() + seconds
        while time.time() < end:
            self.pump(0.1)

    def close(self) -> None:
        try:
            self.send("exit\n")
            self.settle(0.3)
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
