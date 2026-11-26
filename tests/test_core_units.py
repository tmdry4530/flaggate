"""Audit chain, planner, simulation/rollback, approval keys, sealed secrets, CLI TTY guard, socket daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core_fakes import CFG, Env, demo_snapshot
from flaggate.approval import ApprovalError, generate_keypair, load_private_key, load_public_key, make_payload, sign_token, verify_token
from flaggate.audit import AuditLog
from flaggate.diff import diff_states, render_text
from flaggate.plan import SetMvWeightsOp, plan_hash
from flaggate.planner import PlannerError, plan_from_prompt, split_clauses
from flaggate.rollback import generate_rollback, rollback_plan
from flaggate.secretbox import seal, unseal
from flaggate.state import PlanConflict, rules_to_api, simulate, state_hash

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- audit
def test_audit_detects_edit_delete_reorder(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(4):
        log.append("e", i=i)
    assert log.verify()[0]
    lines = log.path.read_text().splitlines()
    for mutated in (lines[:1] + lines[2:], [lines[1], lines[0], *lines[2:]],
                    [lines[0], lines[1].replace('"i":1', '"i":9'), *lines[2:]]):
        log.path.write_text("\n".join(mutated) + "\n")
        assert not log.verify()[0]
    log.path.write_text("\n".join(lines) + "\n")
    assert log.verify() == (True, 4, f"chain OK, 4 records, head {json.loads(lines[-1])['record_hash'][:12]}")


def test_audit_file_is_private(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.append("e")
    assert oct(log.path.stat().st_mode & 0o777) == "0o600"


# ----------------------------------------------------------------------------- planner
def test_planner_rollout_creates_feature_segment_override():
    p = plan_from_prompt("roll out dark_mode to 10% of pro users", demo_snapshot())
    assert [o.op for o in p.ops] == ["create_feature", "create_segment", "set_segment_override"]
    conds = p.ops[1].rules[0].conditions
    assert [(c.property, c.operator, c.value) for c in conds] == [("plan", "EQUAL", "pro"), ("", "PERCENTAGE_SPLIT", "10")]
    assert p.ops[2].segment == f"@{p.ops[1].ref}"
    assert p.base_state_hash == state_hash(demo_snapshot()) and p.guardrails.max_exposure_pct == 20


def test_planner_gradual_rollout_updates_existing_segment():
    snap = demo_snapshot()
    p1 = plan_from_prompt("roll out new_checkout to 10% of beta users", snap)
    after = simulate(snap, p1.ops)
    p2 = plan_from_prompt("roll out new_checkout to 50% of beta users with max exposure 60%", after)
    assert [o.op for o in p2.ops] == ["update_segment_rules"] and p2.guardrails.max_exposure_pct == 60


def test_planner_ab_test_and_promote():
    snap = demo_snapshot()
    p = plan_from_prompt("A/B test checkout_button_color green vs orange 50/50", snap)
    assert p.ops[-1].weights == {"blue": 0.0, "green": 50.0, "orange": 50.0}
    p2 = plan_from_prompt("promote green for checkout_button_color", simulate(snap, p.ops))
    assert p2.ops[0].weights == {"blue": 0.0, "green": 100.0, "orange": 0.0}
    p3 = plan_from_prompt("a/b test checkout_button_color green vs purple", snap)
    assert [o.op for o in p3.ops] == ["add_variant", "set_mv_weights"]
    p4 = plan_from_prompt("ab test hero_copy short vs long 30/70", snap)
    assert p4.ops[0].op == "create_mv_feature" and p4.ops[0].weights == {"short": 30.0, "long": 70.0}


def test_planner_misc_and_errors():
    snap = demo_snapshot()
    ops = plan_from_prompt("disable new_checkout, set checkout_banner to 'Free shipping over $30'", snap).ops
    assert (ops[0].op, ops[0].enabled) == ("set_flag", False) and ops[1].value == "Free shipping over $30"
    assert plan_from_prompt("set checkout_banner to 42", snap).ops[0].value == 42
    with pytest.raises(PlannerError, match="could not understand"):
        plan_from_prompt("make it pop", snap)
    with pytest.raises(PlannerError, match="unknown feature"):
        plan_from_prompt("enable nope", snap)
    done = simulate(snap, plan_from_prompt("roll out new_checkout to 100% of pro users", snap).ops)
    with pytest.raises(PlannerError, match="already live"):
        plan_from_prompt("roll out new_checkout to 100% of pro users", done)
    assert split_clauses("a; b\nc") == ["a", "b", "c"]


def test_planner_stop_rollout():
    snap = demo_snapshot()
    after = simulate(snap, plan_from_prompt("roll out new_checkout to 10% of beta users", snap).ops)
    p = plan_from_prompt("stop rollout of new_checkout", after)
    assert [o.op for o in p.ops] == ["delete_segment_override", "delete_segment"]


# ----------------------------------------------------------------------------- simulation / rollback / diff
def test_simulate_rejects_bad_ops():
    snap = demo_snapshot()
    with pytest.raises(PlanConflict, match="every variant"):
        simulate(snap, [SetMvWeightsOp(op="set_mv_weights", feature="checkout_button_color",
                                       weights={"green": 50, "orange": 50})])
    with pytest.raises(PlanConflict, match="unknown feature"):
        simulate(snap, plan_from_prompt("disable new_checkout", snap).model_copy(
            update={"ops": [plan_from_prompt("disable new_checkout", snap).ops[0].model_copy(update={"feature": "x"})]}).ops)


@pytest.mark.parametrize("prompt", [
    "roll out new_checkout to 10% of beta users; a/b test checkout_button_color green vs orange 50/50",
    "disable new_checkout, set checkout_banner to bye",
    "a/b test checkout_button_color green vs purple 20/80",
])
def test_rollback_restores_state_except_new_features(prompt):
    snap = demo_snapshot()
    plan = plan_from_prompt(prompt, snap)
    rb = generate_rollback(snap, list(plan.ops))
    restored = simulate(simulate(snap, plan.ops), rb.ops)
    if "purple" in prompt:  # v1 cannot delete a variant: it stays at 0%
        restored.features["checkout_button_color"].variants.pop("purple")
        restored.features["checkout_button_color"].env.weights.pop("purple")
    assert state_hash(restored) == state_hash(snap)


def test_rollback_of_created_feature_disables_and_archives():
    snap = demo_snapshot()
    plan = plan_from_prompt("roll out dark_mode to 10% of pro users", snap)
    rb = generate_rollback(snap, list(plan.ops))
    assert [o.op for o in rb.ops] == ["delete_segment_override", "delete_segment", "set_flag", "archive_feature"]
    f = simulate(simulate(snap, plan.ops), rb.ops).features["dark_mode"]
    assert f.archived and not f.env.enabled and not f.overrides
    assert any("archives" in n for n in rb.irreversible)


def test_rollback_plan_is_deterministic():
    snap = demo_snapshot()
    plan = plan_from_prompt("disable new_checkout", snap)
    plan = plan.model_copy(update={"rollback": plan.rollback.model_copy(update={"ops": generate_rollback(snap, list(plan.ops)).ops})})
    assert plan_hash(rollback_plan(plan)) == plan_hash(rollback_plan(plan.model_copy(deep=True)))
    assert rollback_plan(plan).base_state_hash == f"post:{plan_hash(plan)}"


def test_diff_render_and_api_rules():
    snap = demo_snapshot()
    plan = plan_from_prompt("roll out new_checkout to 10% of beta users", snap)
    text = render_text(diff_states(snap, simulate(snap, plan.ops)))
    assert "+ segments/new_checkout_rollout_beta  plan == beta AND 10% split" in text
    assert "+ features/new_checkout/overrides/new_checkout_rollout_beta" in text
    api = rules_to_api(simulate(snap, plan.ops).segments["new_checkout_rollout_beta"].rules)
    split = api[0]["conditions"][1]
    assert split == {"property": None, "operator": "PERCENTAGE_SPLIT", "value": "10"}, "null, not '' (v2.295.2 rejects '')"


# ----------------------------------------------------------------------------- approval keys / secrets
def test_keygen_roundtrip_and_wrong_passphrase(tmp_path):
    generate_keypair(tmp_path / "k.enc", tmp_path / "k.pub", "correct horse")
    assert oct((tmp_path / "k.enc").stat().st_mode & 0o777) == "0o600"
    assert b"PRIVATE KEY" in (tmp_path / "k.enc").read_bytes() and b"ENCRYPTED" in (tmp_path / "k.enc").read_bytes()
    with pytest.raises(ApprovalError):
        load_private_key(tmp_path / "k.enc", "wrong horse!")
    key = load_private_key(tmp_path / "k.enc", "correct horse")
    plan = plan_from_prompt("disable new_checkout", demo_snapshot())
    tok = sign_token(key, make_payload(plan, "apply", "me", 15))
    assert verify_token(load_public_key(tmp_path / "k.pub"), tok)["plan_hash"] == plan_hash(plan)
    with pytest.raises(ApprovalError):
        verify_token(load_public_key(tmp_path / "k.pub"), tok[:-4] + "AAAA")
    with pytest.raises(ValueError):
        generate_keypair(tmp_path / "k2.enc", tmp_path / "k2.pub", "short")


def test_secretbox(tmp_path):
    seal(tmp_path / "s.enc", {"admin_api_key": "abc"}, "gatekeeper pass")
    assert b"abc" not in (tmp_path / "s.enc").read_bytes()
    assert unseal(tmp_path / "s.enc", "gatekeeper pass") == {"admin_api_key": "abc"}
    with pytest.raises(ValueError, match="wrong"):
        unseal(tmp_path / "s.enc", "nope nope nope")


# ----------------------------------------------------------------------------- CLI TTY guard
@pytest.mark.parametrize("args", [["approve", "pl_x"], ["keygen"], ["gatekeeper", "start"]])
def test_cli_refuses_without_tty(tmp_path, args):
    env = {**os.environ, "FG_HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "flaggate.cli", *args], input="7f3a\npassphrase\n",
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert r.returncode == 2, r.stderr
    assert "needs an interactive terminal" in r.stderr
    assert not (tmp_path / "approvals").exists() and not (tmp_path / "approver").exists()


# ----------------------------------------------------------------------------- daemon over unix socket
def test_daemon_socket_roundtrip(tmp_path):
    from flaggate.server import GatekeeperClient, GatekeeperError, serve

    env = Env(tmp_path)
    sock = tmp_path / "gk.sock"
    srv = serve(env.gk, sock)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert oct(sock.stat().st_mode & 0o777) == "0o600"
        c = GatekeeperClient(sock)
        assert c.health()["ok"]
        b = c.plan_from_prompt("disable new_checkout")
        with pytest.raises(GatekeeperError) as e:
            c.apply(b["plan_id"], None)
        assert e.value.status == 403 and e.value.body["reason"] == "no_valid_approval"
        plan = env.gk.get_plan(b["plan_id"])
        r = c.apply(b["plan_id"], env.token(plan))
        assert r["ok"] and r["status"] == "applied"
        assert c.audit_verify()["ok"]
        with pytest.raises(GatekeeperError) as e:
            c.plan_from_prompt("make it pop")
        assert e.value.status == 400
        assert CFG.env_key not in json.dumps(c.review(b["plan_id"])["plan"])
    finally:
        srv.shutdown()
        srv.server_close()
