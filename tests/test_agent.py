"""dev2/agent: fg-mcp cannot apply/approve, agent socket, promote, manual-steps accounting (offline)."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core_fakes import CFG, Env, demo_snapshot
from flaggate.mcp_server import TOOL_NAMES, MCPServer, Tools, scrub_environment
from flaggate.metrics import format_elapsed, manual_steps
from flaggate.planner import plan_from_prompt
from flaggate.promote import PromoteError, analyze, promote_plan, read_metrics, two_proportion_p
from flaggate.server import GatekeeperClient, GatekeeperError, serve

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def daemon(tmp_path):
    """Gatekeeper on fakes with both sockets, like `fg gatekeeper start`."""
    env = Env(tmp_path)
    lock = threading.Lock()
    human, agent = serve(env.gk, tmp_path / "gk.sock", "human", lock), serve(env.gk, tmp_path / "agent.sock", "agent", lock)
    for s in (human, agent):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield env, GatekeeperClient(tmp_path / "gk.sock"), GatekeeperClient(tmp_path / "agent.sock")
    for s in (human, agent):
        s.shutdown()
        s.server_close()


def rpc(server: MCPServer, method: str, params: dict | None = None, mid: int = 1) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def tool(server: MCPServer, name: str, **args) -> tuple[dict, bool]:
    r = rpc(server, "tools/call", {"name": name, "arguments": args})["result"]
    return json.loads(r["content"][0]["text"]), r["isError"]


# ----------------------------------------------------------------------------- the agent side cannot apply/approve
def test_tool_list_has_no_apply_or_approve():
    assert TOOL_NAMES == ["snapshot", "read_features", "read_feature", "read_segments", "plan_schema",
                          "plan_from_prompt", "submit_plan", "plan_status", "wait_for_result"]
    for name in TOOL_NAMES:
        assert not any(w in name for w in ("apply", "approve", "rollback", "sign", "token", "key"))
    assert not any(hasattr(Tools, n) for n in ("apply", "approve", "rollback"))
    from flaggate.mcp_server import TOOL_SPECS
    ann = {t["name"]: t["annotations"] for t in TOOL_SPECS}
    assert all(ann[n]["readOnlyHint"] for n in TOOL_NAMES if n not in ("plan_from_prompt", "submit_plan"))
    assert ann["submit_plan"] == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                                  "openWorldHint": False}


def test_mcp_module_never_touches_keys():
    """fg-mcp imports neither the Flagsmith client, the sealed secrets, nor the approver private-key code."""
    tree = ast.parse((ROOT / "flaggate/mcp_server.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not mods & {"flaggate.client", "flaggate.secretbox", "flaggate.approval", "flaggate.gatekeeper",
                       "flaggate.flagsmith_io", "flaggate.executor"}
    code = subprocess.run([sys.executable, "-c", "import sys, flaggate.mcp_server; print(sorted(m for m in sys.modules "
                           "if m.startswith('flaggate')))"], capture_output=True, text=True, cwd=ROOT, check=True).stdout
    for forbidden in ("flaggate.client", "flaggate.secretbox", "flaggate.approval", "flaggate.executor",
                      "flaggate.gatekeeper", "flaggate.flagsmith_io"):
        assert forbidden not in code


def test_scrub_environment(monkeypatch):
    monkeypatch.setenv("FS_ADMIN_API_KEY", "x" * 40)
    monkeypatch.setenv("FLAGSMITH_API_KEY", "y" * 40)
    assert set(scrub_environment()) >= {"FS_ADMIN_API_KEY", "FLAGSMITH_API_KEY"}
    assert "FS_ADMIN_API_KEY" not in os.environ


def test_agent_socket_has_no_apply_or_rollback_route(daemon):
    env, human, agent = daemon
    b = agent.plan_from_prompt("disable new_checkout", actor="agent:mcp")
    plan = env.gk.get_plan(b["plan_id"])
    good = env.token(plan)  # even a VALID human token is useless through the agent socket
    for fn in (agent.apply, agent.rollback):
        with pytest.raises(GatekeeperError) as e:
            fn(b["plan_id"], good)
        assert e.value.status == 404
    assert env.gk.meta(b["plan_id"])["status"] == "submitted"
    assert env.gk.audit.records()[-1]["reason"] == "agent_socket"
    assert human.apply(b["plan_id"], good)["ok"]  # the human path still works with the same token


def test_mcp_end_to_end_with_fake_gatekeeper(daemon):
    env, human, agent = daemon
    srv = MCPServer(Tools(agent, sleep=lambda s: None))
    init = rpc(srv, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and "cannot apply" in init["result"]["instructions"]
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert [t["name"] for t in rpc(srv, "tools/list")["result"]["tools"]] == TOOL_NAMES

    snap, err = tool(srv, "snapshot")
    assert not err and snap["base_state_hash"].startswith("sha256:") and "new_checkout" in snap["features"]
    assert snap["segments"]["pro_users"] == "plan == pro"

    out, err = tool(srv, "apply", plan_id="x")
    assert err and "cannot apply" in out["error"]

    sub, err = tool(srv, "plan_from_prompt", prompt="roll out new_checkout to 10% of beta users")
    assert not err and sub["approvable"] and "fg approve" in sub["next_step"] and sub["manual_steps_replaced"] > 0
    pid = sub["plan_id"]
    st, _ = tool(srv, "wait_for_result", plan_id=pid, timeout_s=0)
    assert st["done"] is False and st["status"] == "submitted"

    # agent-written plan through submit_plan
    tmpl, _ = tool(srv, "plan_schema")
    p = plan_from_prompt("disable checkout_banner", env.world.snapshot())
    d = json.loads(p.model_dump_json(by_alias=True))
    assert d["base_state_hash"] == tmpl["template"]["base_state_hash"] and d["target"] == tmpl["template"]["target"]
    sub2, err = tool(srv, "submit_plan", plan=d)
    assert not err and sub2["plan_id"] == p.plan_id

    # a forward delete_feature from the agent is refused by the gatekeeper
    d2 = {**d, "plan_id": "pl_evil", "ops": [{"op": "delete_feature", "feature": "new_checkout"}]}
    out, err = tool(srv, "submit_plan", plan=d2)
    assert err and out["reason"] == "delete_feature_not_allowed"

    # human approves + applies on the human socket; the agent sees the result
    plan = env.gk.get_plan(pid)
    assert human.apply(pid, env.token(plan))["ok"]
    res, _ = tool(srv, "wait_for_result", plan_id=pid, timeout_s=1)
    assert res["done"] and res["status"] == "applied" and res["result"]["manual_steps_replaced"] > 0
    assert "elapsed_s" in res["result"] and res["last_event"]["event"] == "applied"
    assert CFG.env_key not in json.dumps([snap, sub, res])


def test_fg_mcp_stdio_process(daemon, tmp_path):
    env, _, _ = daemon
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_features", "arguments": {}}}]
    e = {k: v for k, v in os.environ.items()} | {"FG_AGENT_SOCKET": str(tmp_path / "agent.sock"),
                                                 "FS_ADMIN_API_KEY": "should-be-scrubbed"}
    r = subprocess.run([sys.executable, "-m", "flaggate.mcp_server"], input="\n".join(map(json.dumps, msgs)) + "\n",
                       capture_output=True, text=True, env=e, cwd=ROOT, timeout=60)
    out = [json.loads(l) for l in r.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2, 3]
    assert out[0]["result"]["protocolVersion"] == "2024-11-05"
    assert "new_checkout" in json.loads(out[2]["result"]["content"][0]["text"])["features"]


# ----------------------------------------------------------------------------- promote
def _csv(tmp_path, rows, header="variant,exposures,conversions"):
    p = tmp_path / "metrics.csv"
    p.write_text(header + "\n" + "\n".join(rows) + "\n")
    return p


def test_promote_picks_significant_winner(tmp_path):
    snap = demo_snapshot()
    m = _csv(tmp_path, ["blue,500,40", "green,500,70", "orange,500,45"])
    an = analyze(snap, "checkout_button_color", read_metrics(m))
    assert an.winner == "green" and an.control == "blue" and an.significant and an.p_value < 0.01
    plan = promote_plan(snap, an, m)
    assert [o.op for o in plan.ops] == ["set_mv_weights"]
    assert plan.ops[0].weights == {"blue": 0.0, "green": 100.0, "orange": 0.0}
    assert "evidence: blue 8.0% (40/500), green 14.0% (70/500)" in plan.prompt and "metrics sha256" in plan.prompt


def test_promote_refuses_insignificant_unless_forced(tmp_path):
    snap = demo_snapshot()
    m = _csv(tmp_path, ["blue,200,16", "green,200,18", "orange,200,15"])
    an = analyze(snap, "checkout_button_color", read_metrics(m))
    assert an.winner == "green" and not an.significant
    with pytest.raises(PromoteError, match="not significant"):
        promote_plan(snap, an, m)
    assert "[FORCED]" in promote_plan(snap, an, m, force=True).prompt


def test_promote_events_format_and_errors(tmp_path):
    snap = demo_snapshot()
    rows = [f"u{i},{'green' if i % 2 else 'blue'},{1 if (i % 2 and i % 3 == 0) else 0}" for i in range(400)]
    m = _csv(tmp_path, rows, "identity,variant,converted")
    c = read_metrics(m)
    assert c["green"][0] == 200 and c["blue"] == (200, 0)
    with pytest.raises(PromoteError, match="does not have"):
        analyze(snap, "checkout_button_color", {"purple": (100, 5)})
    with pytest.raises(PromoteError, match="not a multivariate"):
        analyze(snap, "new_checkout", c)
    with pytest.raises(PromoteError, match="exposures yet"):
        analyze(snap, "checkout_button_color", {"blue": (10, 1)})
    assert two_proportion_p(100, 10, 100, 10) == pytest.approx(1.0)


def test_promote_plan_goes_through_the_gate(daemon, tmp_path):
    env, human, _ = daemon
    m = _csv(tmp_path, ["blue,500,40", "green,500,70", "orange,500,45"])
    snap = env.world.snapshot()
    plan = promote_plan(snap, analyze(snap, "checkout_button_color", read_metrics(m)), m)
    b = human.submit(json.loads(plan.model_dump_json(by_alias=True)), actor="fg:promote")
    with pytest.raises(GatekeeperError) as e:
        human.apply(b["plan_id"], None)
    assert e.value.status == 403


# ----------------------------------------------------------------------------- manual steps
def test_manual_steps_counts():
    snap = demo_snapshot()
    p = plan_from_prompt("roll out dark_mode to 10% of pro users", snap)
    ms = manual_steps(p)
    # create_feature 3 + create_segment 2+2 conditions + override 3 + impact check 1
    assert ms["dashboard_steps"] == 10 and ms["manual_steps_replaced"] == 11
    assert format_elapsed(42.4) == "42s" and format_elapsed(125) == "2m05s" and format_elapsed(None) == "n/a"
