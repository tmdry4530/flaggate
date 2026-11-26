"""Gatekeeper refusal paths (offline, fake Flagsmith world) + two-phase apply + rollback."""
from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_fakes import Env
from flaggate.gatekeeper import Refused
from flaggate.planner import plan_from_prompt
from flaggate.rollback import rollback_plan
from flaggate.state import state_hash

PROMPT = "roll out new_checkout to 10% of beta users; a/b test checkout_button_color green vs orange 50/50"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def submitted(env: Env, prompt: str = PROMPT):
    plan = plan_from_prompt(prompt, env.world.snapshot())
    bundle = env.gk.submit(plan)
    return env.gk.get_plan(plan.plan_id), bundle


def refused_reason(fn, *a) -> str:
    with pytest.raises(Refused) as e:
        fn(*a)
    return e.value.reason


def last_event(env: Env) -> dict:
    return env.gk.audit.records()[-1]


def test_submit_attaches_rollback_impact_and_audits(env):
    plan, b = submitted(env)
    assert b["plan_hash"] == plan.plan_hash
    assert plan.rollback.ops, "gatekeeper generates rollback ops"
    assert plan.impact["engine"] == "fake"
    assert "+ segments/new_checkout_rollout_beta" in b["diff_text"]
    assert last_event(env)["event"] == "plan_submitted"


def test_apply_without_token_refused(env):
    plan, _ = submitted(env)
    assert refused_reason(env.gk.apply, plan.plan_id, None) == "no_valid_approval"
    assert last_event(env)["event"] == "refused" and last_event(env)["reason"] == "no_valid_approval"
    assert env.executor.phases == []


def test_forged_signature_refused(env):
    plan, _ = submitted(env)
    forged = env.token(plan, key=Ed25519PrivateKey.generate())
    assert refused_reason(env.gk.apply, plan.plan_id, forged) == "bad_signature"


def test_token_for_other_plan_hash_refused(env):
    plan, _ = submitted(env)
    other = plan.model_copy(update={"prompt": "something else"})  # different hash, same id
    assert refused_reason(env.gk.apply, plan.plan_id, env.token(other)) == "hash_mismatch"


def test_tampered_stored_plan_refused(env):
    plan, _ = submitted(env)
    path = env.gk._plan_path(plan.plan_id)
    d = json.loads(path.read_text())
    d["ops"][0]["name"] = "evil_feature"
    path.write_text(json.dumps(d))
    assert refused_reason(env.gk.apply, plan.plan_id, env.token(plan)) == "tampered"


def test_token_reuse_refused(env):
    plan, _ = submitted(env)
    tok = env.token(plan)
    assert env.gk.apply(plan.plan_id, tok)["ok"]
    assert refused_reason(env.gk.apply, plan.plan_id, tok) == "token_reused"


def test_expired_token_refused(env):
    plan, _ = submitted(env)
    tok = env.token(plan, now=env.now - 3600, ttl_min=15)
    assert refused_reason(env.gk.apply, plan.plan_id, tok) == "expired"


def test_drift_refused(env):
    plan, _ = submitted(env)
    tok = env.token(plan)
    env.world.snap.features["checkout_banner"].env.value = "changed by someone else"
    assert refused_reason(env.gk.apply, plan.plan_id, tok) == "drift"
    assert env.executor.phases == []


def test_wrong_action_refused(env):
    plan, _ = submitted(env)
    assert refused_reason(env.gk.apply, plan.plan_id, env.token(plan, action="rollback")) == "wrong_action"


def test_stale_plan_refused_at_submit(env):
    plan = plan_from_prompt(PROMPT, env.world.snapshot())
    env.world.snap.features["checkout_banner"].env.enabled = False
    assert refused_reason(env.gk.submit, plan) == "stale"


def test_guardrails_refused_before_any_write(tmp_path):
    env = Env(tmp_path, approvable=False)
    plan, b = submitted(env)
    assert b["approvable"] is False
    assert refused_reason(env.gk.apply, plan.plan_id, env.token(plan)) == "guardrails"
    assert env.executor.phases == []


def test_two_phase_order_and_verify(env):
    plan, _ = submitted(env)
    r = env.gk.apply(plan.plan_id, env.token(plan))
    assert r["ok"] and r["two_phase"]
    assert env.executor.phases[0] == ["create_segment"]
    assert "create_segment" not in env.executor.phases[1]
    assert env.impact.calls == ["preview", "preview", "repredict", "verify"]
    rec = last_event(env)
    assert rec["event"] == "applied" and rec["prediction_match"] == "10/10"
    assert rec["result_state_hash"] == state_hash(env.world.snap)


def test_halt_when_live_ids_break_guardrail(tmp_path):
    env = Env(tmp_path, repredict_approvable=False)
    before = env.world.snapshot()
    plan, _ = submitted(env)
    r = env.gk.apply(plan.plan_id, env.token(plan))
    assert not r["ok"] and r["status"] == "halted" and r["compensated"]
    assert env.executor.phases == [["create_segment"]], "phase 2 never ran"
    assert state_hash(env.world.snap) == state_hash(before), "created segments were removed"
    rec = last_event(env)
    assert rec["event"] == "halted" and rec["reason"] == "guardrails_after_live_ids"
    assert rec["approved_max_exposure_pct"] == 5.0 and rec["live_ids_max_exposure_pct"] == 30.0
    assert refused_reason(env.gk.apply, plan.plan_id, env.token(plan)) == "already_used"


def test_single_phase_without_new_segments(env):
    plan, _ = submitted(env, "disable new_checkout")
    r = env.gk.apply(plan.plan_id, env.token(plan))
    assert r["ok"] and not r["two_phase"]
    assert "repredict" not in env.impact.calls


def test_pre_approved_rollback_restores_and_is_single_use(env):
    before = env.world.snapshot()
    plan, _ = submitted(env)
    rb_tok = env.token(rollback_plan(plan), action="rollback", ttl_min=24 * 60)
    assert env.gk.apply(plan.plan_id, env.token(plan))["ok"]
    r = env.gk.rollback(plan.plan_id, rb_tok)
    assert r["ok"] and r["restored_base_state"]
    assert state_hash(env.world.snap) == state_hash(before)
    assert refused_reason(env.gk.rollback, plan.plan_id, rb_tok) == "token_reused"


def test_rollback_token_cannot_apply_and_apply_token_cannot_rollback(env):
    plan, _ = submitted(env)
    apply_tok = env.token(plan)
    assert env.gk.apply(plan.plan_id, apply_tok)["ok"]
    assert refused_reason(env.gk.rollback, plan.plan_id, env.token(plan, action="rollback")) == "hash_mismatch"


def test_rollback_refused_after_drift(env):
    plan, _ = submitted(env)
    rb_tok = env.token(rollback_plan(plan), action="rollback", ttl_min=60)
    assert env.gk.apply(plan.plan_id, env.token(plan))["ok"]
    env.world.snap.features["checkout_banner"].env.value = "hotfix"
    assert refused_reason(env.gk.rollback, plan.plan_id, rb_tok) == "drift"


def test_archive_note_on_review(env):
    _, b = submitted(env, "archive checkout_banner")
    assert any("keeps serving" in n for n in b["rollback_notes"])
    assert "no effect on evaluation" in b["diff_text"]


def test_audit_chain_verifies_after_mixed_events(env):
    plan, _ = submitted(env)
    with pytest.raises(Refused):
        env.gk.apply(plan.plan_id, None)
    env.gk.apply(plan.plan_id, env.token(plan))
    r = env.gk.audit_verify()
    assert r["ok"] and r["records"] == 4 and r["live_matches_last_change"] is True
    assert [x["event"] for x in env.gk.audit.records()] == ["plan_submitted", "refused", "approved", "applied"]
    # tamper with one record: verification fails
    path = env.gk.audit.path
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1]); rec["reason"] = "nothing to see"; lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    assert env.gk.audit_verify()["ok"] is False
