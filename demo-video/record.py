#!/usr/bin/env python3
"""Record a REAL `fg` session on the local self-hosted Flagsmith, then render it to gif + mp4.

    demo-video/setup-tools.sh                       # once: asciinema (venv), checks agg + ffmpeg
    .venv/bin/python demo-video/record.py test-clip # ~20 s spike clip -> demo-video/clips/test-clip.{cast,gif,mp4}

What it does (all local, no accounts, no API keys, nothing leaves the box):
  1. creates a THROW-AWAY Flagsmith organisation/project/environment (never touches 'Toggle Wars Demo'),
     seeds 200 identities, creates an approver key and a gatekeeper in a temp FG_HOME (/tmp/fgdemo-*);
  2. runs demo-video/scenario.py under `asciinema rec` (real `fg` commands, typed like a human);
  3. cast -> gif (agg) -> mp4 (ffmpeg, H.264 yuv420p);
  4. tears everything down: stops the gatekeeper, deletes the throw-away organisation and the temp dir.
Passphrases are random per run, only held in memory/env of the child processes, never printed or recorded.

Outputs live in demo-video/clips/ (NOT dist/out/build, which are wiped on box replacement).
"""
from __future__ import annotations

import argparse
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
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from flaggate.approval import generate_keypair  # noqa: E402
from flaggate.flagsmith_io import api_url_from_env, load_credentials, server_reachable  # noqa: E402
from flaggate.identities import Identity  # noqa: E402
from flaggate.verify import sdk_live_eval  # noqa: E402
from live_helpers import LiveProject  # noqa: E402

COLS, ROWS = 116, 38
FONT_SIZE = 18


def fg_bin() -> str:
    cand = ROOT / ".venv" / "bin" / "fg"
    if not cand.exists():
        sys.exit(f"{cand} not found: run `UV_PROJECT_ENVIRONMENT=$PWD/.venv uv sync --extra dev` first")
    return str(cand)


def with_fd(secret: str):
    r, w = os.pipe()
    os.write(w, (secret + "\n").encode())
    os.close(w)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="test-clip")
    ap.add_argument("--speed", type=float, default=1.0, help="agg playback speed")
    ap.add_argument("--idle", type=float, default=2.0, help="agg: cap idle gaps to N seconds")
    ap.add_argument("--identities", type=int, default=200, help="identities seeded into the throw-away org")
    ap.add_argument("--no-render", action="store_true", help="record the .cast only")
    args = ap.parse_args()

    api, creds = api_url_from_env(), load_credentials()
    if not server_reachable(api) or not all(k in creds for k in ("FS_ADMIN_EMAIL", "FS_ADMIN_PASSWORD")):
        sys.exit("local Flagsmith not reachable or credentials missing (scripts/selfhost-up.sh)")
    asciinema = HERE / ".tools" / "bin" / "asciinema"
    if not asciinema.exists():
        sys.exit("run demo-video/setup-tools.sh first")
    clips = HERE / "clips"
    clips.mkdir(exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="fgdemo-"))
    os.chmod(tmp, 0o700)
    home, sock = tmp / "home", tmp / "gk.sock"
    gk = None
    lp = LiveProject(api, creds["FS_ADMIN_EMAIL"], creds["FS_ADMIN_PASSWORD"])
    try:
        lp.create()
        seed = sdk_live_eval(api, lp.env_key, transient=False)
        for i in range(args.identities):
            seed(Identity(f"user-{i}", {"plan": "pro" if i % 5 < 2 else "free",
                                        "country": ("KR", "US", "DE")[i % 3]}))
        env_name = lp.admin.results(f"/api/v1/environments/?project={lp.project_id}")[0]["name"]
        envfile = tmp / "runtime.env"
        fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"FS_ADMIN_API_KEY={lp.admin.key}\nFS_ENV_KEY={lp.env_key}\nFS_SERVER_KEY={lp.server_key}\n")

        approver_pass, gk_pass = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
        adir = home / "approver"
        adir.mkdir(parents=True)
        generate_keypair(adir / "approver.key.enc", adir / "approver.pub", approver_pass)
        (adir / "approver.json").write_text('{"name": "demo-human"}')

        env = {**os.environ, "FG_HOME": str(home), "FG_SOCKET": str(sock), "FG_BIN": fg_bin(),
               "FG_DEMO_APPROVER_PASS": approver_pass, "SCENARIO": args.scenario, "TERM": "xterm-256color",
               "PATH": f"{ROOT / '.venv' / 'bin'}:{os.environ['PATH']}"}
        for k in [k for k in env if k.startswith("FS_")]:
            env.pop(k)
        fd_init = with_fd(gk_pass)
        subprocess.run([fg_bin(), "gatekeeper", "init", "--api-url", api, "--organisation-id", str(lp.org),
                        "--project-id", str(lp.project_id), "--environment", env_name,
                        "--from-env-file", str(envfile), "--passphrase-fd", str(fd_init)],
                       env=env, pass_fds=(fd_init,), check=True, stdout=subprocess.DEVNULL)
        os.close(fd_init)
        envfile.unlink()                                  # the gatekeeper has sealed the keys; remove plaintext
        fd_start = with_fd(gk_pass)
        gk = subprocess.Popen([fg_bin(), "gatekeeper", "start", "--passphrase-fd", str(fd_start)], env=env,
                              pass_fds=(fd_start,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.close(fd_start)
        for _ in range(100):
            if sock.exists():
                break
            if gk.poll() is not None:
                sys.exit("gatekeeper exited: " + (gk.stderr.read().decode()[-300:] if gk.stderr else ""))
            time.sleep(0.1)
        else:
            sys.exit("gatekeeper socket did not appear")

        cast = clips / f"{args.scenario}.cast"
        t0 = time.time()
        subprocess.run([str(asciinema), "rec", "--overwrite", "--quiet", "--cols", str(COLS), "--rows", str(ROWS),
                        "--command", f"{sys.executable} {HERE / 'scenario.py'}", str(cast)], env=env, check=True,
                       stdin=subprocess.DEVNULL if not sys.stdin.isatty() else None)
        print(f"recorded {cast} in {time.time() - t0:.1f}s wall")
        if not args.no_render:
            gif, mp4 = cast.with_suffix(".gif"), cast.with_suffix(".mp4")
            subprocess.run(["agg", "--font-size", str(FONT_SIZE), "--idle-time-limit", str(args.idle),
                            "--speed", str(args.speed), "--font-family",
                            "Geist Mono,DejaVu Sans Mono,Liberation Mono", str(cast), str(gif)], check=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(gif), "-movflags", "+faststart",
                            "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264",
                            "-crf", "20", str(mp4)], check=True)
            print(f"rendered {gif.name} ({gif.stat().st_size // 1024} KiB), {mp4.name} ({mp4.stat().st_size // 1024} KiB)")
    finally:
        if gk is not None and gk.poll() is None:
            gk.send_signal(signal.SIGINT)
            try:
                gk.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gk.kill()
        lp.delete()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
