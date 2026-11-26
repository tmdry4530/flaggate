"""dev2/core end-to-end against the local self-hosted Flagsmith (127.0.0.1:8600).

plan (rule-based planner) -> submit (diff + dev3 impact preview) -> approve (Ed25519 key fixture; the CLI's
TTY check is untouched and tested separately) -> two-phase apply -> live SDK verify -> pre-approved rollback.
Plus: an injected mid-apply failure is compensated in Flagsmith. Each test uses a throw-away organisation
(tests/live_helpers.LiveProject) that is deleted afterwards; the 'Toggle Wars Demo' org is never touched.
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flaggate.approval import make_payload, public_bytes, sign_token
from flaggate.client import FlagsmithClient
from flaggate.executor import Executor
from flaggate.flagsmith_io import api_url_from_env, load_credentials, server_reachable
from flaggate.gatekeeper import Gatekeeper, GatekeeperConfig, Refused
from flaggate.hooks import FlagsmithImpact
from flaggate.identities import Identity
from flaggate.planner import plan_from_prompt
from flaggate.rollback import rollback_plan
from flaggate.state import state_hash
from flaggate.verify import sdk_live_eval

from live_helpers import LiveProject

pytestmark = pytest.mark.integration
API = api_url_from_env()
CREDS = load_credentials()
if not server_reachable(API) or not all(k in CREDS for k in ("FS_ADMIN_EMAIL", "FS_ADMIN_PASSWORD")):
    pytest.skip("local Flagsmith not reachable / credentials missing", allow_module_level=True)

IDENTS = [Identity(f"core-it-{i}", {"plan": ("pro" if i % 5 < 2 else "free"), "country": "KR"}) for i in range(60)]


@pytest.fixture
def world(tmp_path):
    lp = LiveProject(API, CREDS["FS_ADMIN_EMAIL"], CREDS["FS_ADMIN_PASSWORD"]).create()
    try:
        client = FlagsmithClient(API, lp.admin.key, secrets=(lp.env_key, lp.server_key))
        env_name = client.environment(lp.env_key)["name"]
        cfg = GatekeeperConfig(api_url=API, organisation_id=lp.org, project_id=lp.project_id, environment=env_name,
                               env_key=lp.env_key, server_key=lp.server_key)
        key = Ed25519PrivateKey.generate()
        impact = FlagsmithImpact(api_url=API, env_key=lp.env_key, server_key=lp.server_key,
                                 identities=lambda: IDENTS, sample_source="core-it identities")
        yield {"lp": lp, "client": client, "cfg": cfg, "key": key, "impact": impact, "tmp": tmp_path}
    finally:
        lp.delete()


def make_gk(w, **kw) -> Gatekeeper:
    return Gatekeeper(state_dir=w["tmp"] / "gk", config=w["cfg"], approver_pub=public_bytes(w["key"]),
                      client=w["client"], impact=w["impact"], **kw)


def test_plan_approve_apply_verify_rollback(world):
    gk = make_gk(world)
    base = gk.snapshot()
    prompt = ("roll out new_checkout to 50% of pro users; "
              "a/b test checkout_button_color green vs orange 50/50 with max exposure 45%")
    b = gk.submit(plan_from_prompt(prompt, base))
    plan = gk.get_plan(b["plan_id"])
    assert b["approvable"], b["impact"].get("blockers")
    assert [o.op for o in plan.ops] == ["create_segment", "set_segment_override", "set_mv_weights"]
    assert "+ segments/new_checkout_rollout_pro" in b["diff_text"]

    with pytest.raises(Refused) as e:
        gk.apply(plan.plan_id, None)
    assert e.value.reason == "no_valid_approval"

    apply_tok = sign_token(world["key"], make_payload(plan, "apply", "it-human", 15))
    rb_tok = sign_token(world["key"], make_payload(rollback_plan(plan), "rollback", "it-human", 60))
    r = gk.apply(plan.plan_id, apply_tok)
    assert r["ok"] and r["two_phase"], r
    assert r["verify"]["ok"] and r["verify"]["prediction_match"] == "60/60", r["verify"]

    # independent read-back with the official SDK: free users never get new_checkout, weights moved
    ev = sdk_live_eval(API, world["lp"].env_key, transient=True)
    assert not any(ev(i)["new_checkout"]["enabled"] for i in IDENTS if i.traits["plan"] == "free")
    assert any(ev(i)["new_checkout"]["enabled"] for i in IDENTS if i.traits["plan"] == "pro")
    after = gk.snapshot()
    assert after.features["checkout_button_color"].env.weights == {"blue": 0.0, "green": 50.0, "orange": 50.0}
    assert after.segments["new_checkout_rollout_pro"].rules[0]["conditions"][1]["operator"] == "PERCENTAGE_SPLIT"

    with pytest.raises(Refused) as e:
        gk.apply(plan.plan_id, apply_tok)
    assert e.value.reason == "token_reused"

    rr = gk.rollback(plan.plan_id, rb_tok)
    assert rr["ok"] and rr["restored_base_state"], rr
    assert state_hash(gk.snapshot()) == state_hash(base)
    assert not any(ev(i)["new_checkout"]["enabled"] for i in IDENTS)

    v = gk.audit_verify()
    assert v["ok"] and v["live_matches_last_change"]
    assert [x["event"] for x in gk.audit.records()] == ["plan_submitted", "refused", "approved", "applied",
                                                        "refused", "rolled_back"]


class _FailingExecutor(Executor):
    def __init__(self, client, env_key, fail_at):
        super().__init__(client, env_key)
        self.fail_at = fail_at

    def session(self, before):
        s = super().session(before)
        s.fail_at = self.fail_at
        return s


def test_mid_apply_failure_is_compensated(world):
    gk = make_gk(world, executor_factory=lambda: _FailingExecutor(world["client"], world["cfg"].env_key, fail_at=2))
    base = gk.snapshot()
    b = gk.submit(plan_from_prompt("roll out dark_mode to 10% of pro users", base))
    plan = gk.get_plan(b["plan_id"])
    assert [o.op for o in plan.ops] == ["create_feature", "create_segment", "set_segment_override"]
    r = gk.apply(plan.plan_id, sign_token(world["key"], make_payload(plan, "apply", "it-human", 15)))
    assert not r["ok"] and r["status"] == "apply_failed" and r["compensated"], r
    now = gk.snapshot()
    assert "dark_mode" not in now.features and "dark_mode_rollout_pro" not in now.segments
    assert state_hash(now) == state_hash(base)
    rec = gk.audit.records()[-1]
    assert rec["event"] == "apply_failed" and rec["compensated"] is True and rec["phase"] == 2
    methods = [c["method"] for c in rec["flagsmith_calls"]]
    assert methods.count("POST") == 2 and methods.count("DELETE") == 2  # created feature + segment, both removed


def test_rollback_deletes_newly_created_feature(world):
    gk = make_gk(world)
    base = gk.snapshot()
    b = gk.submit(plan_from_prompt("roll out dark_mode to 100% of pro users", base))
    plan = gk.get_plan(b["plan_id"])
    assert [o.op for o in plan.rollback.ops][-1] == "delete_feature"
    rb_tok = sign_token(world["key"], make_payload(rollback_plan(plan), "rollback", "it-human", 60))
    assert gk.apply(plan.plan_id, sign_token(world["key"], make_payload(plan, "apply", "it-human", 15)))["ok"]
    assert "dark_mode" in gk.snapshot().features
    rr = gk.rollback(plan.plan_id, rb_tok)
    assert rr["ok"] and rr["restored_base_state"], rr
    names = [f["name"] for f in world["client"].list_all(f"/api/v1/projects/{world['lp'].project_id}/features/")]
    assert "dark_mode" not in names
    # a fresh plan can create the same name again (Flagsmith really freed it)
    again = gk.submit(plan_from_prompt("roll out dark_mode to 100% of pro users", gk.snapshot()))
    assert again["plan"]["ops"][0]["op"] == "create_feature"
