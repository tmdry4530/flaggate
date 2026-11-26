#!/usr/bin/env python3
"""The two recorded terminals of the final video. Run by build.py inside `asciinema rec`:

    scenes.py agent   terminal A: the coding agent (real `codex exec` with only the fg-mcp tool server)
    scenes.py human   terminal B: the human release manager (fg apply refused, fg approve, promote, rollback, audit)

Both are real interactive shells (typist.py). They talk to the gatekeeper like any user would; the only
coordination between them is "a plan has appeared" and "the agent is done" (marker files in the private temp dir).
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from typist import Typist, marker, out  # noqa: E402

COLS, ROWS = int(os.environ["DEMO_COLS"]), int(os.environ["DEMO_ROWS"])
TMP = Path(os.environ["DEMO_TMP"])
PLANS = TMP / "h" / "gatekeeper" / "state" / "plans"
ENV = {k[5:]: v for k, v in os.environ.items() if k.startswith("DEMO_E_")}   # DEMO_E_X -> X in the shell

TASK_LINES = ("Product request: roll out the new checkout (feature new_checkout) to 25% of pro users",
              "and A/B test the button colour (feature checkout_button_color): green 30%, orange 30%, blue 40%.",
              "Keep exposure under 20% of users.")
TASK = " ".join(TASK_LINES)
RULES = ("You have the flaggate MCP tools. Use ONLY those tools: look at the state with snapshot, create and "
         "submit the change with plan_from_prompt (one short clause per change), report the plan_id, the diff "
         "and the impact verdict, and say that a human must run fg approve. You cannot apply or approve "
         "yourself. Then call wait_for_result until done=true and finish with a short report: final status, "
         "prediction match, manual steps replaced and elapsed time.")
AGENT_FN = r'''
RULES='__RULES__'
agent() { env -u OPENAI_API_KEY codex exec --ephemeral --ignore-user-config --skip-git-repo-check -s read-only \
  -c 'mcp_servers.flaggate.command="fg-mcp"' -c "mcp_servers.flaggate.env={FG_AGENT_SOCKET=\"$FG_AGENT_SOCKET\"}" \
  -c mcp_servers.flaggate.tool_timeout_sec=120 "You are a release engineer. $TASK $RULES"; }
'''.replace("__RULES__", RULES.replace("'", "'\\''"))


def submitted_plan() -> str | None:
    import json
    if PLANS.exists():
        for meta in sorted(PLANS.glob("*.meta.json")):
            if json.loads(meta.read_text()).get("status") == "submitted":
                return meta.name[: -len(".meta.json")]
    return None


def wait_for(cond, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.3)
    raise SystemExit(f"timed out waiting for {what}")


def shell(rc_extra: str = "") -> Typist:
    out("\x1b[2J\x1b[H")
    return Typist({**{k: v for k, v in os.environ.items() if not k.startswith(("DEMO_", "ASCIINEMA"))}, **ENV},
                  cwd=str(TMP / "work"), cols=COLS, rows=ROWS, rc_extra=rc_extra)


def agent() -> None:
    mode = os.environ.get("DEMO_AGENT", "codex")
    t = shell(AGENT_FN)
    marker("a_start")
    t.settle(0.8)
    t.run("env | grep -ciE 'key|token|secret|password'")        # the agent's shell holds nothing sensitive
    marker("a_nokeys")
    t.quiet(0.6)
    if mode == "codex":
        t.run("declare -f agent")
        marker("a_fn")
        t.quiet(0.4)
        t.type('TASK="' + TASK_LINES[0], cps=70)                  # a three-line assignment, as a person would type it
        t.type(TASK_LINES[1], cps=70)
        t.run(TASK_LINES[2] + '"', cps=70)
        marker("a_task")
        t.type('agent')
        marker("a_codex_start")
    else:                                                       # fallback: the keyless rule planner, same gate
        t.type('FG_SOCKET=$FG_AGENT_SOCKET fg plan "roll out new_checkout to 25% of pro users; '
               'a/b test checkout_button_color green vs orange 30/30 keeping exposure under 20%"')
        marker("a_codex_start")
    seen_plan = False
    end = time.time() + 600
    while time.time() < end:
        t.pump(0.2)
        if not seen_plan and submitted_plan():
            seen_plan = True
            marker("a_plan_visible")
        if re.search(r"(?:^|\n)\$ $", t.seen):
            break
    marker("a_done")
    (TMP / "agent.done").write_text("1")
    t.quiet(1.0)
    t.close()


def human() -> None:
    t = shell()
    marker("h_start")
    pid = wait_for(submitted_plan, 600, "the agent's plan")
    marker("h_plan_seen")
    t.settle(0.2)
    t.run(f"FG_SOCKET=$FG_AGENT_SOCKET fg apply {pid}")        # the agent's door: no apply route behind it
    marker("h_refused")
    t.quiet(0.4)
    approve(t, pid, "h")
    wait_for(lambda: (TMP / "agent.done").exists(), 300, "the agent to finish")
    marker("h_agent_done")
    t.quiet(0.5)
    t.type("clear", enter=True)
    t.wait_prompt()
    marker("h_promote_start")
    t.run("mock-traffic --feature checkout_button_color --rates blue=0.08,green=0.13,orange=0.09 "
          "--n 1500 --out metrics.csv", timeout=300, cps=45)
    marker("h_traffic_done")
    t.quiet(0.5)
    txt = t.run("fg promote checkout_button_color --metrics metrics.csv", timeout=120)
    marker("h_promote_plan")
    pid2 = re.search(r"^plan\s+(\S+)", txt, re.M).group(1)
    approve(t, pid2, "h2")
    t.quiet(0.5)
    t.type("clear")
    t.wait_prompt()
    marker("h_rollback_start")
    t.run(f"fg rollback {pid2}", timeout=120)
    marker("h_rolled_back")
    t.quiet(0.4)
    t.run("fg audit verify")
    marker("h_audit_verified")
    t.quiet(0.4)
    t.run("fg audit show")
    marker("h_audit_shown")
    t.quiet(1.0)
    t.close()


def approve(t: Typist, pid: str, tag: str) -> None:
    t.run("clear")
    t.type(f"fg approve {pid}")
    t.expect(r"Type the first 4 characters of the plan hash[^\n]*: ", 120)
    text = t.before
    h = re.search(r"^hash\s+([0-9a-f]{64})", text, re.M).group(1)
    marker(f"{tag}_review")
    marker("hold:3")                                    # the editor also holds the final prompt for a moment
    t.quiet(0.2)
    t.type(h[:4], cps=4)
    t.expect(r"Approver passphrase: ", 60)
    t.quiet(0.4)
    t.type(os.environ["DEMO_APPROVER_PASS"], cps=14)     # echo is off (getpass): nothing appears
    t.expect(r"(?:APPLIED|NOT APPLIED)[^\n]*\n", 180)
    marker(f"{tag}_applied")
    t.wait_prompt(60)


if __name__ == "__main__":
    {"agent": agent, "human": human}[sys.argv[1]]()
