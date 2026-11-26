import copy
import json

import pytest

from flaggate.identities import Identity
from flaggate.impact import compute_impact, evaluate_identities, render_text
from flaggate.verify import compare_predictions, sdk_offline_eval


def flip(report, feature, **kw):
    out = [f for f in report["flips"] if f["feature"] == feature and all(f[k] == v for k, v in kw.items())]
    return out


def test_demo_plan_report_shape(env_doc, plan, identities):
    r = compute_impact(env_doc, plan, identities, sample_source="fixtures").report
    assert r["engine"].startswith("flagsmith-flag-engine ") and r["sample"] == {"source": "fixtures", "n": 200}
    assert r["evaluated"] and r["approvable"] and r["guardrails_ok"] and r["blockers"] == []
    json.dumps(r)  # JSON-safe
    (nc,) = flip(r, "new_checkout")
    assert (nc["from"], nc["to"], nc["kind"]) == (False, True, "toggle")
    assert nc["by_segment"] == {"pro_users_10pct": nc["count"]}
    assert 1 <= nc["count"] <= 12 and nc["pct"] == round(100 * nc["count"] / 200, 1)
    assert r["changed"] + r["unchanged"] == 200


def test_rollout_flips_only_pro_identities_in_the_split(env_doc, plan, identities):
    res = compute_impact(env_doc, plan, identities, max_listed=1000)
    (nc,) = flip(res.report, "new_checkout")
    traits = {i.identifier: i.traits for i in identities}
    assert all(traits[who]["plan"] == "pro" for who in nc["identifiers"])
    # identity override user-7 is already ON before the plan, so it never shows as a flip
    assert "user-7" not in nc["identifiers"]
    assert res.before["user-7"].flags["new_checkout"]["enabled"] is True
    # about 10% of the pro identities
    pros = sum(1 for i in identities if i.traits["plan"] == "pro")
    assert abs(nc["count"] - pros * 0.10) <= 4


def test_variant_shift_reported_but_not_a_guardrail_by_default(env_doc, plan, identities):
    r = compute_impact(env_doc, plan, identities).report
    cb = flip(r, "checkout_button_color")
    assert cb and all(f["kind"] == "variant" for f in cb)
    assert r["features"]["checkout_button_color"]["variant_shift_pct"] > r["guardrails"]["max_exposure_pct"]
    assert r["max_exposure_feature"] == "new_checkout" and r["approvable"]


def test_variant_shift_guardrail_when_set(env_doc, plan, identities):
    r = compute_impact(env_doc, plan, identities, max_variant_shift_pct=10).report
    assert r["guardrails"]["max_variant_shift_pct"] == 10
    assert not r["approvable"] and not r["guardrails_ok"]
    assert any("variant shift" in v for v in r["guardrails"]["violations"])


def test_exposure_guardrail_exceeded_marks_not_approvable(env_doc, plan, identities):
    plan["ops"][0]["rules"][0]["conditions"][1]["value"] = "100"   # all pro users
    r = compute_impact(env_doc, plan, identities).report
    assert r["max_exposure_pct"] > 20 and r["guardrails_ok"] is False and r["approvable"] is False
    assert any(b.startswith("guardrail:") and "new_checkout" in b for b in r["blockers"])
    assert "NOT APPROVABLE" in render_text(r) and "EXCEEDED" in render_text(r)


def test_guardrail_boundary_is_inclusive(env_doc, identities, make_plan):
    ops = [{"op": "set_flag", "feature": "new_checkout", "enabled": True}]
    # everyone except identity override user-7 flips: 199/200 = 99.5%
    assert compute_impact(env_doc, make_plan(ops, 99.5), identities).report["approvable"] is True
    assert compute_impact(env_doc, make_plan(ops, 99.4), identities).report["approvable"] is False


def test_empty_sample_is_not_approvable(env_doc, plan):
    r = compute_impact(env_doc, plan, []).report
    assert not r["approvable"] and any("no identities" in b for b in r["blockers"])


def test_unappliable_plan_is_not_approvable_and_not_evaluated(env_doc, identities, make_plan):
    r = compute_impact(env_doc, make_plan([{"op": "set_flag", "feature": "ghost", "enabled": True}]), identities).report
    assert not r["evaluated"] and not r["approvable"] and r["flips"] == [] and r["unchanged"] is None
    assert "plan cannot be applied" in r["blockers"][0]
    assert "Could not evaluate" in render_text(r)


def test_noop_plan_warns(env_doc, identities, make_plan):
    r = compute_impact(env_doc, make_plan([{"op": "set_flag", "feature": "new_checkout", "enabled": False}]),
                       identities).report
    assert r["changed"] == 0 and r["approvable"] and r["warnings"]
    assert "nobody" in render_text(r)


def test_value_change_and_attribution_to_environment_default(env_doc, identities, make_plan):
    r = compute_impact(env_doc, make_plan([{"op": "set_flag", "feature": "checkout_banner", "enabled": True,
                                            "value": "Hello"}]), identities).report
    flips = flip(r, "checkout_banner")
    assert {f["kind"] for f in flips} == {"value"}
    by = {k for f in flips for k in f["by_segment"]}
    # beta_testers keep their own banner (segment override beats the env default): only others flip
    assert by == {"(environment default)"}
    beta = sum(1 for i in identities if i.traits.get("beta") is True)
    assert sum(f["count"] for f in flips) == 200 - beta - 0


def test_segment_override_flip_attributed_to_segment(env_doc, identities, make_plan):
    p = make_plan([{"op": "set_segment_override", "feature": "new_checkout", "segment": "pro_users", "enabled": True}])
    r = compute_impact(env_doc, p, identities).report
    pros = sum(1 for i in identities if i.traits["plan"] == "pro")
    (nc,) = flip(r, "new_checkout")
    assert nc["by_segment"] == {"pro_users": nc["count"]} and nc["count"] == pros   # user-7 already on, is it pro?
    seg = {s["segment"]: s for s in r["segments"]}["pro_users"]
    assert seg["members_before"] == seg["members_after"] == pros and seg["entered"] == seg["left"] == 0
    assert seg["flips"] == nc["count"]


def test_segment_membership_aggregates_for_new_segment(env_doc, plan, identities):
    r = compute_impact(env_doc, plan, identities).report
    s = {x["segment"]: x for x in r["segments"]}["pro_users_10pct"]
    (nc,) = flip(r, "new_checkout")
    assert (s["members_before"], s["members_after"], s["entered"], s["left"]) == (0, nc["count"], nc["count"], 0)
    assert s["pct_after"] == nc["pct"]


def test_plan_rollback_ops_restore_the_before_state(env_doc, plan, identities, make_plan):
    """The plan's own rollback ops (@refs and all), applied on top of the forward plan, give back today's state."""
    from flaggate import Plan
    fwd = compute_impact(env_doc, plan, identities)
    # the rollback ops refer to @seg_pro_10, created by the forward plan: replay both in one go
    both = make_plan(Plan.model_validate(plan).model_dump(mode="json")["ops"]
                     + Plan.model_validate(plan).model_dump(mode="json")["rollback"]["ops"])
    back = compute_impact(env_doc, both, identities)
    assert back.after_document["project"]["segments"] == env_doc["project"]["segments"]
    assert back.predictions() == {i: {n: {"enabled": f["enabled"], "value": f["value"]} for n, f in e.flags.items()}
                                  for i, e in fwd.before.items()}
    assert back.report["changed"] == 0


def test_new_disabled_feature_is_not_a_flip(env_doc, identities, make_plan):
    r = compute_impact(env_doc, make_plan([{"op": "create_feature", "name": "brand_new"}]), identities).report
    assert r["flips"] == [] and r["changed"] == 0
    r = compute_impact(env_doc, make_plan([{"op": "create_feature", "name": "brand_new", "default_enabled": True}]),
                       identities).report
    (f,) = flip(r, "brand_new")
    assert (f["from"], f["to"], f["count"]) == (False, True, 200) and not r["approvable"]


def test_input_document_and_identities_not_mutated(env_doc, plan, identities):
    snap = copy.deepcopy(env_doc)
    compute_impact(env_doc, plan, identities)
    assert env_doc == snap


def test_deterministic(env_doc, plan, identities):
    a = compute_impact(env_doc, plan, identities).report
    b = compute_impact(env_doc, plan, identities).report
    a.pop("env_document_at"), b.pop("env_document_at")
    assert a == b


def test_engine_matches_official_sdk_offline(env_doc, plan, identities):
    """Independent cross-check: the SDK in offline mode (its own mapping + engine call) serves exactly
    what our evaluation predicts, before and after the plan."""
    res = compute_impact(env_doc, plan, identities)
    before_pred = {i: {n: {"enabled": f["enabled"], "value": f["value"]} for n, f in e.flags.items()}
                   for i, e in res.before.items()}
    assert compare_predictions(before_pred, identities, sdk_offline_eval(env_doc)).ok
    assert compare_predictions(res.predictions(), identities, sdk_offline_eval(res.after_document)).ok


def test_evaluate_identities_matches_hand_checked_cases(env_doc):
    ev = evaluate_identities(env_doc, [Identity("a", {"plan": "pro", "beta": True}), Identity("user-7", {})])
    assert ev["a"].segments == ("beta_testers", "pro_users")
    assert ev["a"].flags["checkout_banner"]["value"] == "Beta: free returns"
    assert ev["user-7"].segments == ("identity_overrides",) and ev["user-7"].flags["new_checkout"]["enabled"]


def test_text_output_is_human_readable(env_doc, plan, identities):
    text = compute_impact(env_doc, plan, identities, sample_source="fixtures").render()
    for needle in ("IMPACT PREVIEW", "WHO CHANGES", "new_checkout  OFF → ON", "via pro_users_10pct",
                   "GUARDRAILS", "VERDICT: APPROVABLE", "assumption:", "e.g. user-"):
        assert needle in text, needle
    assert "FIXTURE_ENV_KEY" not in text


def test_impact_in_plan_does_not_change_plan_hash(env_doc, plan, identities):
    from flaggate import Plan, plan_hash
    from flaggate.impact import attach_impact
    p = Plan.model_validate(plan)
    h = plan_hash(p)
    assert h == "78dd43e38a6d86fe7a598facf69cc0ad1a8d4fb24909d851227ef3e487e35adf"      # golden, docs/PLAN_SCHEMA.md
    res = compute_impact(env_doc, p, identities)
    with_impact = attach_impact(p, res)
    assert p.impact is None and with_impact.impact is not None           # original untouched, copy carries it
    assert with_impact.impact["plan_hash"] == h == res.report["plan_hash"]
    assert plan_hash(with_impact) == h
    # and it survives a JSON round trip of the whole plan
    again = Plan.model_validate_json(with_impact.model_dump_json(by_alias=True))
    assert plan_hash(again) == h and again.impact["sample"]["n"] == 200


def test_dict_plans_are_validated_and_bad_ones_rejected(env_doc, plan, identities):
    import pydantic
    assert compute_impact(env_doc, plan, identities).approvable          # dict accepted
    plan["ops"] = []
    with pytest.raises(pydantic.ValidationError):
        compute_impact(env_doc, plan, identities)


def test_at_refs_are_resolved_in_op_order(env_doc, plan, identities, make_plan):
    from flaggate import Plan
    ops = Plan.model_validate(plan).model_dump(mode="json")["ops"]
    swapped = [ops[1], ops[0], ops[2]]                                    # override before the segment exists
    r = compute_impact(env_doc, make_plan(swapped), identities).report
    assert not r["approvable"] and "unknown segment reference '@seg_pro_10'" in r["blockers"][0]


def _split_plan(make_plan, pct, guard=20, with_trait=False):
    conds = ([{"property": "plan", "operator": "EQUAL", "value": "pro"}] if with_trait else []) + \
            [{"property": "", "operator": "PERCENTAGE_SPLIT", "value": str(pct)}]
    return make_plan([
        {"op": "create_segment", "ref": "s", "name": "ramp", "rules": [{"type": "ALL", "conditions": conds}]},
        {"op": "set_segment_override", "feature": "new_checkout", "segment": "@s", "enabled": True}], guard)


def test_id_sensitivity_reports_range_around_the_guess(env_doc, plan, identities):
    r = compute_impact(env_doc, plan, identities).report
    s = r["id_sensitivity"]
    assert s["segments"] == ["pro_users_10pct"] and s["candidates"] == 17
    assert s["exposure_pct"]["min"] <= r["max_exposure_pct"] <= s["exposure_pct"]["max"]
    assert s["exposure_pct"]["max"] < 20 and r["approvable"]


def test_id_sensitivity_blocks_a_split_that_can_exceed_the_guardrail(env_doc, identities, make_plan):
    # an 18% split of everyone: the guessed id may land under 20%, but other ids the server may assign do not
    p = _split_plan(make_plan, 18)
    r = compute_impact(env_doc, p, identities).report
    assert r["id_sensitivity"]["exposure_pct"]["max"] > 20
    assert not r["approvable"] and any("depending on the server-assigned id" in b for b in r["blockers"])
    assert "segment id still unknown" in render_text(r)


def test_known_segment_id_or_disabled_sampling_skips_sensitivity(env_doc, plan, identities):
    assert compute_impact(env_doc, plan, identities, id_samples=0).report["id_sensitivity"] is None
    hinted = compute_impact(env_doc, plan, identities, hints={"segment_ids": {"pro_users_10pct": 17}})
    assert hinted.report["id_sensitivity"] is None and hinted.report["assumptions"] == []


def test_no_split_no_sensitivity(env_doc, identities, make_plan):
    p = make_plan([{"op": "set_segment_override", "feature": "new_checkout", "segment": "pro_users", "enabled": True}])
    assert compute_impact(env_doc, p, identities).report["id_sensitivity"] is None
