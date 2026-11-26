"""A throw-away FlagGate stack for the final video: own Flagsmith organisation (deleted at the end), 200 seeded
identities, an approver key, a gatekeeper with a human socket and an agent socket, and a short private directory.

Nothing here touches the 'Toggle Wars Demo' organisation; the only Flagsmith credentials used are the local
superuser login (read in this process from the runtime's local.env, never passed on to child processes).
"""
from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from flaggate.approval import generate_keypair  # noqa: E402
from flaggate.flagsmith_io import api_url_from_env, load_credentials, server_reachable  # noqa: E402
from flaggate.identities import Identity  # noqa: E402
from flaggate.verify import sdk_live_eval  # noqa: E402
from live_helpers import LiveProject  # noqa: E402

FORBIDDEN = ("FS_", "FLAGSMITH", "FLAGGATE", "FG_", "OPENAI", "ANTHROPIC", "GITHUB", "GH_")


def dirty_env_vars() -> list[str]:
    """Names of inherited variables that must not be in the recording environment (names only, never values)."""
    return sorted(k for k in os.environ
                  if k.startswith(FORBIDDEN) or k.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")))


def clean_env(**extra: str) -> dict[str, str]:
    """The only environment any recorded process gets."""
    venv_bin = str(ROOT / ".venv" / "bin")
    env = {"PATH": f"{venv_bin}:{Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
           "LANG": "C.UTF-8", "TERM": "xterm-256color"}
    env.update(extra)
    return env


def with_fd(secret: str) -> int:
    r, w = os.pipe()
    os.write(w, (secret + "\n").encode())
    os.close(w)
    return r


class DemoStack:
    def __init__(self, identities: int = 200):
        self.identities = identities
        self.lp: LiveProject | None = None
        self.gk: subprocess.Popen | None = None
        self.tmp = Path(tempfile.mkdtemp(prefix="fg-", dir="/tmp"))
        os.chmod(self.tmp, 0o700)
        self.home = self.tmp / "h"
        self.work = self.tmp / "work"
        self.bin = self.tmp / "bin"
        self.approver_pass = secrets.token_urlsafe(12)
        self.gk_pass = secrets.token_urlsafe(12)

    # -- paths the scenes use
    @property
    def agent_sock(self) -> Path:
        return self.home / "gatekeeper" / "agent.sock"

    @property
    def plans_dir(self) -> Path:
        return self.home / "gatekeeper" / "state" / "plans"

    def env(self) -> dict[str, str]:
        return clean_env(FG_HOME=str(self.home), FG_AGENT_SOCKET=str(self.agent_sock),
                         PATH=f"{self.bin}:{clean_env()['PATH']}")

    # -- life cycle
    def __enter__(self) -> "DemoStack":
        api, creds = api_url_from_env(), load_credentials()
        if not server_reachable(api) or not all(k in creds for k in ("FS_ADMIN_EMAIL", "FS_ADMIN_PASSWORD")):
            sys.exit("local Flagsmith not reachable or credentials missing (scripts/selfhost-up.sh)")
        self.work.mkdir()
        self.bin.mkdir()
        self.lp = LiveProject(api, creds["FS_ADMIN_EMAIL"], creds["FS_ADMIN_PASSWORD"])
        try:
            self.lp.create()
            seed = sdk_live_eval(api, self.lp.env_key, transient=False)     # intentional: seeds the throw-away env
            for i in range(self.identities):
                seed(Identity(f"user-{i}", {"plan": "pro" if i % 5 < 2 else "free", "country": ("KR", "US", "DE")[i % 3]}))
            env_name = self.lp.admin.results(f"/api/v1/environments/?project={self.lp.project_id}")[0]["name"]
            adir = self.home / "approver"
            adir.mkdir(parents=True)
            generate_keypair(adir / "approver.key.enc", adir / "approver.pub", self.approver_pass)
            (adir / "approver.json").write_text('{"name": "release-manager"}')
            # keys for the gatekeeper (sealed with its passphrase), and the client key for the demo traffic only
            envfile = self.tmp / "gk.env"
            fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"FS_ADMIN_API_KEY={self.lp.admin.key}\nFS_ENV_KEY={self.lp.env_key}\n"
                         f"FS_SERVER_KEY={self.lp.server_key}\n")
            fd = os.open(self.tmp / "traffic.env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"FS_ENV_KEY={self.lp.env_key}\n")
            env = self.env()
            fd_init = with_fd(self.gk_pass)
            subprocess.run(["fg", "gatekeeper", "init", "--api-url", api, "--organisation-id", str(self.lp.org),
                            "--project-id", str(self.lp.project_id), "--environment", env_name,
                            "--from-env-file", str(envfile), "--passphrase-fd", str(fd_init)],
                           env=env, pass_fds=(fd_init,), check=True, stdout=subprocess.DEVNULL)
            os.close(fd_init)
            envfile.unlink()
            fd_start = with_fd(self.gk_pass)
            self.gk = subprocess.Popen(["fg", "gatekeeper", "start", "--passphrase-fd", str(fd_start)], env=env,
                                       pass_fds=(fd_start,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.close(fd_start)
            for _ in range(150):
                if self.agent_sock.exists():
                    break
                if self.gk.poll() is not None:
                    sys.exit("gatekeeper exited: " + (self.gk.stderr.read().decode()[-300:] if self.gk.stderr else ""))
                time.sleep(0.1)
            else:
                sys.exit("gatekeeper socket did not appear")
            # shim so the video shows `mock-traffic ...` (demo data generator; the client key stays in a 0600 file)
            shim = self.bin / "mock-traffic"
            shim.write_text("#!/bin/sh\n"
                            f"FLAGGATE_ENV_FILE={self.tmp / 'traffic.env'} FLAGSMITH_API_URL={api} "
                            f"exec {sys.executable} {ROOT / 'scripts' / 'mock_traffic.py'} \"$@\"\n")
            shim.chmod(0o700)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.gk is not None and self.gk.poll() is None:
            self.gk.send_signal(signal.SIGINT)
            try:
                self.gk.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.gk.kill()
        if self.lp is not None:
            try:
                self.lp.delete()
            except Exception as e:  # noqa: BLE001
                print(f"WARNING: could not delete the throw-away org: {type(e).__name__}", file=sys.stderr)
        shutil.rmtree(self.tmp, ignore_errors=True)
