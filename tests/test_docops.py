import copy

import pytest

from flaggate.docops import apply_ops
from flaggate import Plan


@pytest.fixture(autouse=True)
def _bind(make_plan):
    global _make
    _make = make_plan


def run(doc, *ops, hints=None):
    return apply_ops(doc, _make(list(ops)), hints)


def fs(doc, name):
    return next(f for f in doc["feature_states"] if f["feature"]["name"] == name)


def seg(doc, name):
    return next(s for s in doc["project"]["segments"] if s["name"] == name)


def test_does_not_mutate_input(env_doc, plan):
    snap = copy.deepcopy(env_doc)
    res = apply_ops(env_doc, Plan.model_validate(plan))
    assert env_doc == snap and not res.errors
    assert res.document is not env_doc


def test_demo_plan_ops(env_doc, plan):
    res = apply_ops(env_doc, Plan.model_validate(plan))
    s = seg(res.document, "pro_users_10pct")
    assert s["id"] == 3 and res.created_segment_ids == {"pro_users_10pct": 3}
    conds = s["rules"][0]["conditions"]
    assert conds[0] == {"operator": "EQUAL", "value": "pro", "property_": "plan"}
    assert conds[1]["operator"] == "PERCENTAGE_SPLIT" and conds[1]["property_"] == ""
    assert s["feature_states"][0]["feature"]["name"] == "new_checkout" and s["feature_states"][0]["enabled"] is True
    w = {m["multivariate_feature_option"]["value"]: m["percentage_allocation"]
         for m in fs(res.document, "checkout_button_color")["multivariate_feature_state_values"]}
    assert w == {"green": 50.0, "orange": 50.0}      # control (blue) gets the remainder: 0
    assert any("pro_users_10pct" in a and "id" in a for a in res.assumptions)


def test_segment_id_hint_removes_assumption(env_doc, plan):
    res = apply_ops(env_doc, Plan.model_validate(plan), {"segment_ids": {"pro_users_10pct": 41}})
    assert seg(res.document, "pro_users_10pct")["id"] == 41 and res.assumptions == []


def test_set_flag_sets_enabled_and_value(env_doc):
    res = run(env_doc, {"op": "set_flag", "feature": "new_checkout", "enabled": True},
              {"op": "set_flag", "feature": "checkout_banner", "enabled": False, "value": "Hi"})
    assert fs(res.document, "new_checkout")["enabled"] is True
    assert fs(res.document, "checkout_banner")["feature_state_value"] == "Hi"
    assert fs(res.document, "checkout_banner")["enabled"] is False


def test_create_feature_and_mv_feature(env_doc):
    res = run(env_doc, {"op": "create_feature", "name": "f_new", "default_enabled": True, "initial_value": "v", "ref": "fn"},
              {"op": "create_mv_feature", "name": "m_new", "initial_value": "ctl",
               "weights": {"ctl": 50, "a": 20, "b": 30}})
    assert fs(res.document, "f_new")["enabled"] and fs(res.document, "f_new")["feature"]["type"] == "STANDARD"
    assert res.feature_refs == {"fn": "f_new"}
    m = fs(res.document, "m_new")
    assert m["feature"]["type"] == "MULTIVARIATE" and m["feature_state_value"] == "ctl"
    assert {x["multivariate_feature_option"]["value"]: x["percentage_allocation"]
            for x in m["multivariate_feature_state_values"]} == {"a": 20, "b": 30}    # control is implicit
    ids = [x["id"] for x in m["multivariate_feature_state_values"]]
    assert len(set(ids)) == 2 and min(ids) > 2
    assert any("m_new" in a for a in res.assumptions)


def test_add_variant(env_doc):
    res = run(env_doc, {"op": "add_variant", "feature": "checkout_button_color", "value": "red", "weight": 10})
    vals = {x["multivariate_feature_option"]["value"]: x["percentage_allocation"]
            for x in fs(res.document, "checkout_button_color")["multivariate_feature_state_values"]}
    assert vals["red"] == 10 and len(vals) == 3
    assert "already exists" in run(env_doc, {"op": "add_variant", "feature": "checkout_button_color",
                                             "value": "blue", "weight": 1}).errors[0]      # blue is the control
    assert "> 100" in run(env_doc, {"op": "add_variant", "feature": "checkout_button_color",
                                    "value": "red", "weight": 40}).errors[0]


def test_mv_weight_validation(env_doc):
    # the schema already enforces sum == 100; the doc layer checks the names against the live variants
    assert "every variant" in run(env_doc, {"op": "set_mv_weights", "feature": "checkout_button_color",
                                           "weights": {"blue": 50, "green": 50}}).errors[0]
    assert "every variant" in run(env_doc, {"op": "set_mv_weights", "feature": "checkout_button_color",
                                           "weights": {"blue": 0, "green": 50, "orange": 25, "purple": 25}}).errors[0]
    assert "not a multivariate" in run(env_doc, {"op": "set_mv_weights", "feature": "new_checkout",
                                                "weights": {"a": 100}}).errors[0]


def test_schema_rejects_bad_weights_before_the_engine_sees_them(env_doc):
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        _make([{"op": "set_mv_weights", "feature": "checkout_button_color", "weights": {"green": 33, "orange": 33}}])


def test_control_gets_the_remainder(env_doc):
    ok = run(env_doc, {"op": "set_mv_weights", "feature": "checkout_button_color",
                       "weights": {"blue": 34, "green": 33, "orange": 33}})
    assert not ok.errors
    assert [m["percentage_allocation"] for m in fs(ok.document, "checkout_button_color")["multivariate_feature_state_values"]] == [33, 33]


def test_segment_lifecycle(env_doc):
    rules = [{"type": "ALL", "conditions": [{"property": "country", "operator": "EQUAL", "value": "KR"}]}]
    res = run(env_doc,
              {"op": "create_segment", "ref": "kr", "name": "kr_users", "rules": rules},
              {"op": "set_segment_override", "feature": "new_checkout", "segment": "@kr", "enabled": True},
              {"op": "update_segment_rules", "segment": "kr_users",
               "rules": [{"type": "ALL", "conditions": [{"property": "country", "operator": "EQUAL", "value": "JP"}]}]},
              {"op": "delete_segment_override", "feature": "new_checkout", "segment": "kr_users"})
    assert not res.errors
    s = seg(res.document, "kr_users")
    assert s["feature_states"] == [] and s["rules"][0]["conditions"][0]["value"] == "JP"
    res = run(env_doc, {"op": "create_segment", "ref": "kr", "name": "kr_users", "rules": rules},
              {"op": "delete_segment", "segment": "@kr"})
    assert not res.errors and all(s["name"] != "kr_users" for s in res.document["project"]["segments"])


def test_override_priority_is_taken_from_the_op(env_doc):
    res = run(env_doc, {"op": "set_segment_override", "feature": "checkout_banner", "segment": "pro_users",
                        "enabled": True, "value": "pro banner", "priority": 5})
    assert seg(res.document, "pro_users")["feature_states"][0]["feature_segment"]["priority"] == 5


def test_replace_existing_override(env_doc):
    res = run(env_doc, {"op": "set_segment_override", "feature": "checkout_banner", "segment": "beta_testers",
                        "enabled": False, "value": "x", "priority": 0})
    ov = seg(res.document, "beta_testers")["feature_states"]
    assert len(ov) == 1 and ov[0]["enabled"] is False and ov[0]["feature_segment"]["priority"] == 0


@pytest.mark.parametrize("op, msg", [
    ({"op": "set_flag", "feature": "nope", "enabled": True}, "does not exist"),
    ({"op": "create_feature", "name": "new_checkout"}, "already exists"),
    ({"op": "set_segment_override", "feature": "new_checkout", "segment": "@ghost", "enabled": True}, "unknown segment reference"),
    ({"op": "delete_segment", "segment": "ghost"}, "does not exist"),
    ({"op": "delete_segment_override", "feature": "new_checkout", "segment": "pro_users"}, "no override"),
    ({"op": "create_segment", "name": "pro_users", "rules": []}, "already exists"),
    ({"op": "set_segment_override", "feature": "@nofeat", "segment": "pro_users", "enabled": True}, "unknown feature reference"),
    ({"op": "set_segment_override", "feature": "new_checkout", "segment": "pro_users", "enabled": True,
      "weights": {"a": 100}}, "not multivariate"),
    ({"op": "add_variant", "feature": "new_checkout", "value": "x", "weight": 1}, "not a multivariate"),
    ({"op": "add_variant", "feature": "checkout_button_color", "value": "green", "weight": 1}, "already exists"),
])
def test_op_errors(env_doc, op, msg):
    res = run(env_doc, op)
    assert res.errors and msg in res.errors[0]


def test_first_error_stops_application(env_doc):
    res = run(env_doc, {"op": "set_flag", "feature": "nope", "enabled": True}, {"op": "set_flag", "feature": "new_checkout", "enabled": True})
    assert len(res.errors) == 1 and fs(res.document, "new_checkout")["enabled"] is False


def test_archive_is_a_noted_noop(env_doc):
    res = run(env_doc, {"op": "archive_feature", "feature": "new_checkout"})
    assert res.document["feature_states"] == env_doc["feature_states"] and res.notes


def test_refs_resolve_for_features_and_segments_and_numeric_ids(env_doc):
    rules = [{"type": "ALL", "conditions": [{"property": "country", "operator": "EQUAL", "value": "KR"}]}]
    res = run(env_doc,
              {"op": "create_feature", "name": "fresh", "ref": "f", "default_enabled": False},
              {"op": "create_segment", "name": "kr", "ref": "s", "rules": rules},
              {"op": "set_segment_override", "feature": "@f", "segment": "@s", "enabled": True},
              {"op": "update_segment_rules", "segment": "1", "rules": rules})   # numeric id of pro_users
    assert not res.errors
    assert seg(res.document, "kr")["feature_states"][0]["feature"]["name"] == "fresh"
    assert seg(res.document, "pro_users")["rules"][0]["conditions"][0]["value"] == "KR"


def test_override_with_weights_builds_override_variants(env_doc):
    res = run(env_doc, {"op": "set_segment_override", "feature": "checkout_button_color", "segment": "pro_users",
                        "enabled": True, "value": "blue", "weights": {"blue": 20, "green": 40, "orange": 40}})
    (ov,) = seg(res.document, "pro_users")["feature_states"]
    assert {m["multivariate_feature_option"]["value"]: m["percentage_allocation"]
            for m in ov["multivariate_feature_state_values"]} == {"green": 40, "orange": 40}
