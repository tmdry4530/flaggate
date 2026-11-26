"""dev2/agent live: fg-mcp tools against a real gatekeeper on a throw-away organisation, and the full
A/B -> mock traffic -> fg promote -> approve -> apply loop. Skipped if :8600 is down."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from flaggate.approval import make_payload, sign_token
from flaggate.flagsmith_io import api_url_from_env, load_credentials, server_reachable
from flaggate.identities import Identity
from flaggate.mcp_server import MCPServer, Tools
from flaggate.promote import analyze, promote_plan, read_metrics
from flaggate.server import GatekeeperClient, serve
from flaggate.verify import sdk_live_eval

from test_core_live import IDENTS, make_gk, world  # noqa: F401  (fixture reuse)

pytestmark = pytest.mark.integration
API = api_url_from_env()
CREDS = load_credentials()
if not server_reachable(API) or not all(k in CREDS for k in ("FS_ADMIN_EMAIL", "FS_ADMIN_PASSWORD")):
    pytest.skip("local Flagsmith not reachable / credentials missing", allow_module_level=True)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def daemon(world):  # noqa: F811
    gk = make_gk(world)
    lock = threading.Lock()
    tmp = world["tmp"]
    srvs = [serve(gk, tmp / "gk.sock", "human", lock), serve(gk, tmp / "agent.sock", "agent", lock)]
    for s in srvs:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        yield world, gk, GatekeeperClient(tmp / "gk.sock"), GatekeeperClient(tmp / "agent.sock")
    finally:
        for s in srvs:
            s.shutdown()
            s.server_close()


def _call(srv, name, **args):
    r = srv.call(name, args)
    return json.loads(r["content"][0]["text"]), r["isError"]


def test_mcp_agent_flow_live(daemon):
    w, gk, human, agent = daemon
    srv = MCPServer(Tools(agent))
    secrets = [w["lp"].admin.key, w["lp"].env_key, w["lp"].server_key]
    outputs = []
    snap, err = _call(srv, "snapshot")
    outputs.append(snap)
    assert not err and set(snap["features"]) == {"new_checkout", "checkout_button_color"}
    sub, err = _call(srv, "plan_from_prompt", prompt="roll out new_checkout to 50% of pro users with max exposure 45%")
    outputs.append(sub)
    assert not err and sub["approvable"], sub
    pid = sub["plan_id"]
    out, err = _call(srv, "approve", plan_id=pid)
    assert err
    with pytest.raises(Exception):
        agent.apply(pid, "anything")
    st, _ = _call(srv, "wait_for_result", plan_id=pid, timeout_s=0)
    assert st["status"] == "submitted"
    plan = gk.get_plan(pid)
    r = human.apply(pid, sign_token(w["key"], make_payload(plan, "apply", "it-human", 15)))
    assert r["ok"] and r["manual_steps_replaced"] == 8 and r["elapsed_s"] >= 0
    res, _ = _call(srv, "wait_for_result", plan_id=pid, timeout_s=5)
    outputs.append(res)
    assert res["done"] and res["status"] == "applied" and res["result"]["verify"]["prediction_match"] == "60/60"
    blob = json.dumps(outputs)
    assert not any(s in blob for s in secrets), "no Flagsmith key ever reaches the agent"


def test_ab_traffic_promote_live(daemon):
    w, gk, human, agent = daemon
    lp = w["lp"]
    # 1) the A/B test is already running in LiveProject (blue control 34 / green 33 / orange 33)
    metrics = w["tmp"] / "metrics.csv"
    env = {**os.environ, "FS_ENV_KEY": lp.env_key, "FLAGSMITH_API_URL": API}
    r = subprocess.run([sys.executable, "scripts/mock_traffic.py", "--feature", "checkout_button_color",
                        "--rates", "blue=0.08,green=0.14,orange=0.09", "--n", "900", "--seed", "3",
                        "--out", str(metrics)], capture_output=True, text=True, cwd=ROOT, env=env, timeout=300)
    assert r.returncode == 0, r.stderr
    assert lp.env_key not in r.stdout + r.stderr
    counts = read_metrics(metrics, "checkout_button_color")
    assert set(counts) == {"blue", "green", "orange"} and sum(n for n, _ in counts.values()) == 900
    # 2) promote -> same gate
    snap = gk.snapshot()
    an = analyze(snap, "checkout_button_color", counts)
    assert an.winner == "green" and an.significant, an.summary()
    b = human.submit(json.loads(promote_plan(snap, an, metrics).model_dump_json(by_alias=True)), actor="fg:promote")
    assert b["approvable"], b["impact"].get("blockers")
    plan = gk.get_plan(b["plan_id"])
    res = human.apply(plan.plan_id, sign_token(w["key"], make_payload(plan, "apply", "it-human", 15)))
    assert res["ok"], res
    ev = sdk_live_eval(API, lp.env_key, transient=True)
    assert {ev(Identity(f"after-{i}", {"plan": "free"}))["checkout_button_color"]["value"] for i in range(20)} == {"green"}
