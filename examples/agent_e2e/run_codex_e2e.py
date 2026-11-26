#!/usr/bin/env python3
"""Real-agent end-to-end: Codex CLI -> fg-mcp -> gatekeeper -> (human) fg approve -> Flagsmith.

DEMO AUTOMATION: the "human" approval is played by pty_approve.py so the run is unattended. Everything else is
the production path: Codex only gets fg-mcp (no Flagsmith key, agent socket without apply/rollback routes).

What it does
  1. builds a throw-away organisation on the local Flagsmith (tests/live_helpers.LiveProject) and seeds 60
     identities, so the 'Toggle Wars Demo' org is never touched; deleted at the end
  2. temp FG_HOME: fg keygen (pty), fg gatekeeper init (keys via a 0600 temp file, deleted right after), start
  3. runs `env -u OPENAI_API_KEY codex exec` with --ignore-user-config/--ephemeral and fg-mcp passed via -c
     overrides (the user's ~/.codex/config.toml is not read or modified; its sha256 is checked before/after)
  4. when the agent's plan shows up, plays the human: `fg approve <plan_id>` on a pty
  5. waits for Codex to finish, runs `fg audit verify` and `fg rollback`, writes raw logs to --out (outside the
     repo) and a redacted copy to --evidence
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests"), str(Path(__file__).parent)]

from live_helpers import LiveProject  # noqa: E402
from pty_approve import run as pty_run  # noqa: E402

from flaggate.client import FlagsmithClient  # noqa: E402
from flaggate.flagsmith_io import api_url_from_env, load_credentials  # noqa: E402
from flaggate.identities import Identity  # noqa: E402
from flaggate.verify import sdk_live_eval  # noqa: E402

FG = str(REPO / ".venv/bin/fg")
FG_MCP = str(REPO / ".venv/bin/fg-mcp")
CODEX = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
TASK = ("Product request: roll out the new checkout (feature new_checkout) to 50% of pro users, keeping exposure "
        "under 45% of users.")
PROMPT = f"""You are a release engineer. {TASK}

You have the `flaggate` MCP tools. Use ONLY those tools (no shell commands, no file edits):
1. Look at the current state with `snapshot`.
2. Create and submit the change with `plan_from_prompt` (or write a flaggate.plan/v1 JSON and use `submit_plan`).
3. Report the plan_id, the diff and the impact verdict, and say that a human must run `fg approve <plan_id>`.
   You cannot apply or approve it yourself.
4. Call `wait_for_result` repeatedly until it returns done=true.
5. Finish with a short report: final status, prediction match, manual steps replaced and elapsed time."""


def redact_paths(text: str, out: Path) -> str:
    """Remove machine-specific absolute paths from the evidence copy (raw logs keep them)."""
    text = text.replace(str(out), "<raw-log-dir>").replace(CODEX, "codex").replace(str(REPO), "<repo>")
    text = re.sub(r"/tmp/fg-e2e-[\w.-]+", "<tmpdir>", text)
    return text.replace(str(Path.home()), "~")


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO.parent / "fg-agent-e2e" / f"{datetime.now():%Y%m%d-%H%M%S}"))
    ap.add_argument("--evidence", default=str(REPO / "docs/evidence"))
    ap.add_argument("--keep-org", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    api = api_url_from_env()
    creds = load_credentials()
    codex_cfg = Path.home() / ".codex/config.toml"
    cfg_before = sha(codex_cfg)
    lp = LiveProject(api, creds["FS_ADMIN_EMAIL"], creds["FS_ADMIN_PASSWORD"]).create()
    secrets = [lp.admin.key, lp.env_key, lp.server_key, creds.get("FS_ADMIN_PASSWORD", ""),
               creds.get("FS_ADMIN_API_KEY", ""), creds.get("FS_ENV_KEY", ""), creds.get("FS_SERVER_KEY", "")]
    home = Path(tempfile.mkdtemp(prefix="fg-e2e-home-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("FS_") and k != "OPENAI_API_KEY"}
    env["FG_HOME"] = str(home)
    gk_proc = None
    timeline: list[tuple[float, str]] = []
    t0 = time.time()

    def mark(what: str) -> None:
        timeline.append((round(time.time() - t0, 1), what))
        print(f"[{timeline[-1][0]:7.1f}s] {what}", flush=True)

    try:
        ev = sdk_live_eval(api, lp.env_key, transient=False)  # intentional seeding of the throw-away env
        for i in range(60):
            ev(Identity(f"e2e-user-{i}", {"plan": "pro" if i % 5 < 2 else "free", "country": "KR"}))
        env_name = FlagsmithClient(api, lp.admin.key).environment(lp.env_key)["name"]
        mark(f"throw-away org {lp.org} / project {lp.project_id} / env '{env_name}' ready, 60 identities seeded")

        apw, gpw = "demo approver passphrase", "demo gatekeeper passphrase"
        code, txt = pty_run([FG, "keygen", "--name", "demo-human"], [("passphrase", apw), ("again", apw)], env)
        assert code == 0, txt
        credfile = home / "creds.env"
        fd = os.open(credfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, f"FS_ADMIN_API_KEY={lp.admin.key}\nFS_ENV_KEY={lp.env_key}\nFS_SERVER_KEY={lp.server_key}\n".encode())
        os.close(fd)
        r, w = os.pipe()
        os.write(w, (gpw + "\n").encode())
        os.close(w)
        p = subprocess.run([FG, "gatekeeper", "init", "--organisation-id", str(lp.org), "--project-id",
                            str(lp.project_id), "--environment", env_name, "--from-env-file", str(credfile),
                            "--passphrase-fd", str(r)], env=env, pass_fds=(r,), capture_output=True, text=True)
        os.close(r)
        credfile.unlink()
        assert p.returncode == 0, p.stderr
        r, w = os.pipe()
        os.write(w, (gpw + "\n").encode())
        os.close(w)
        gk_log = open(out / "gatekeeper.log", "w")
        gk_proc = subprocess.Popen([FG, "gatekeeper", "start", "--passphrase-fd", str(r)], env=env, pass_fds=(r,),
                                   stdout=gk_log, stderr=subprocess.STDOUT)
        os.close(r)
        sock = home / "gatekeeper" / "agent.sock"
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.1)
        mark(f"gatekeeper pid {gk_proc.pid} up; agent socket {sock}")

        # ---- the human, played by a pty (DEMO AUTOMATION ONLY)
        approvals: dict[str, str] = {}

        def human() -> None:
            plans = home / "gatekeeper" / "state" / "plans"
            deadline = time.time() + 900
            while time.time() < deadline and not approvals:
                for meta in sorted(plans.glob("*.meta.json")) if plans.exists() else []:
                    m = json.loads(meta.read_text())
                    pid = meta.name[: -len(".meta.json")]
                    if m.get("status") == "submitted" and pid not in approvals:
                        st = json.loads((plans / f"{pid}.json").read_text())
                        if not (st.get("impact") or {}).get("approvable"):
                            approvals[pid] = "skipped: not approvable"
                            mark(f"human: {pid} is NOT APPROVABLE, not approving")
                            continue
                        time.sleep(3)  # a human reads the screen
                        mark(f"human: fg approve {pid}")
                        code, txt = pty_run([FG, "approve", pid], [("first 4 characters", st["plan_hash"][:4]),
                                                                   ("Approver passphrase", apw)], env)
                        (out / "fg-approve.log").write_text(txt)
                        approvals[pid] = f"exit {code}"
                        mark(f"human: fg approve exited {code}")
                time.sleep(1)

        threading.Thread(target=human, daemon=True).start()

        workdir = Path(tempfile.mkdtemp(prefix="fg-e2e-codex-"))
        cmd = ["env", "-u", "OPENAI_API_KEY", CODEX, "exec", "--json", "--ephemeral", "--ignore-user-config",
               "--skip-git-repo-check", "-s", "read-only", "-C", str(workdir),
               "-c", f'mcp_servers.flaggate.command="{FG_MCP}"',
               "-c", f'mcp_servers.flaggate.env={{FG_AGENT_SOCKET="{sock}"}}',
               "-c", "mcp_servers.flaggate.tool_timeout_sec=120",
               "-c", "mcp_servers.flaggate.startup_timeout_sec=30",
               "-o", str(out / "codex-last-message.txt"), PROMPT]
        (out / "codex-command.txt").write_text(" ".join(cmd[:-1]) + " <PROMPT>\n\n" + PROMPT + "\n")
        mark("codex exec started")
        with open(out / "codex.jsonl", "w") as jl, open(out / "codex.stderr.log", "w") as er:
            cp = subprocess.run(cmd, env={**env, "NO_COLOR": "1"}, stdout=jl, stderr=er, timeout=1200)
        mark(f"codex exec exited {cp.returncode}")
        calls = []
        for line in (out / "codex.jsonl").read_text().splitlines():
            ev_ = json.loads(line)
            item = ev_.get("item") or {}
            if ev_.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
                res = item.get("result") or {}
                calls.append({"tool": item.get("tool"), "arguments": item.get("arguments"),
                              "ok": item.get("status") == "completed" and not res.get("isError")
                              and not item.get("error")})
        pfp = [c for c in calls if c["tool"] in ("plan_from_prompt", "submit_plan")]
        summary = {"tool_calls": [c["tool"] for c in calls],
                   "first_plan_call": pfp[0] if pfp else None,
                   "first_try_success": bool(pfp and pfp[0]["ok"]),
                   "plan_calls": len(pfp), "approvals": approvals}
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        mark(f"first plan call ok={summary['first_try_success']} ({len(pfp)} plan call(s)): "
             f"{json.dumps((pfp[0]['arguments'] if pfp else None))}")

        for name, args in (("fg-audit-verify", ["audit", "verify"]), ("fg-audit-show", ["audit", "show"])):
            p = subprocess.run([FG, *args], env=env, capture_output=True, text=True)
            (out / f"{name}.log").write_text(p.stdout + p.stderr)
        applied = [k for k, v in approvals.items() if v == "exit 0"]
        if applied:
            p = subprocess.run([FG, "rollback", applied[0]], env=env, capture_output=True, text=True)
            (out / "fg-rollback.log").write_text(p.stdout + p.stderr)
            mark("fg rollback: " + (p.stdout + p.stderr).strip().splitlines()[-1])
            p = subprocess.run([FG, "audit", "verify"], env=env, capture_output=True, text=True)
            (out / "fg-audit-verify-after-rollback.log").write_text(p.stdout + p.stderr)
        (out / "timeline.json").write_text(json.dumps(timeline, indent=1))
    finally:
        if gk_proc:
            gk_proc.terminate()
            gk_proc.wait(15)
        if not a.keep_org:
            org = lp.org
            lp.delete()
            mark(f"throw-away org {org} deleted")
        shutil.rmtree(home, ignore_errors=True)
        cfg_after = sha(codex_cfg)
        mark(f"~/.codex/config.toml unchanged: {cfg_before == cfg_after}")
        (out / "timeline.json").write_text(json.dumps(timeline, indent=1))

    # ---- redacted evidence copy
    ev_dir = Path(a.evidence) / f"codex-e2e-{out.name}"
    ev_dir.mkdir(parents=True, exist_ok=True)
    email = creds.get("FS_ADMIN_EMAIL", "")
    for f in sorted(out.iterdir()):
        text = f.read_text(errors="replace")
        for s in filter(lambda x: x and len(x) >= 6, secrets + [email]):
            text = text.replace(s, "<redacted>")
        text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", text)
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        text = text.replace(str(home), "$FG_HOME")
        text = redact_paths(text, out)
        (ev_dir / f.name).write_text(text)
    print(f"raw logs: {out}\nredacted copy: {ev_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
