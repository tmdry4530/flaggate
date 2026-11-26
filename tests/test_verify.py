import copy

from flaggate.impact import compute_impact
from flaggate.verify import (compare_predictions, hints_from_live_document, repredict_with_live_ids,
                             sdk_offline_eval, verify_prediction)


def test_prediction_matches_when_live_is_the_after_document(env_doc, plan, identities):
    res = compute_impact(env_doc, plan, identities)
    out = verify_prediction(res, sdk_offline_eval(res.after_document))
    assert out.ok and out.summary() == "prediction matched 200/200" and out.prediction_match == "200/200"
    assert out.mismatches == [] and out.reprediction is None
    assert out.render() == "prediction matched 200/200"


def test_nothing_applied_yet_is_a_mismatch(env_doc, plan, identities):
    res = compute_impact(env_doc, plan, identities)
    out = verify_prediction(res, sdk_offline_eval(env_doc))             # live still serves the old state
    assert not out.ok and out.total == 200
    assert out.matched == 200 - res.report["changed"]
    assert {m.feature for m in out.mismatches} <= {"new_checkout", "checkout_button_color"}
    assert out.summary().startswith("prediction matched ") and "✗ user-" in out.render()


def test_live_error_fails_closed_without_leaking(env_doc, plan, identities):
    res = compute_impact(env_doc, plan, identities)

    def boom(ident):
        raise ConnectionError("http://127.0.0.1:8600 key=SECRETVALUE")

    out = verify_prediction(res, boom)
    assert out.matched == 0 and out.total == 200 and not out.ok
    assert all(m.error == "live evaluation failed (ConnectionError)" for m in out.mismatches)
    assert "SECRET" not in out.render() and "127.0.0.1" not in out.render()


def test_empty_sample_never_verifies(env_doc, plan):
    res = compute_impact(env_doc, plan, [])
    out = verify_prediction(res, lambda i: {})
    assert not out.ok and out.total == 0


def test_unevaluable_preview_is_reported(env_doc, identities, make_plan):
    res = compute_impact(env_doc, make_plan([{"op": "set_flag", "feature": "ghost", "enabled": True}]), identities)
    out = verify_prediction(res, lambda i: {})
    assert not out.ok and "could not be evaluated" in out.diagnosis[0]


def test_value_normalisation(identities):
    ident = identities[:1]
    pred = {ident[0].identifier: {"f": {"enabled": True, "value": None}, "g": {"enabled": True, "value": "10"},
                                  "h": {"enabled": True, "value": True}}}
    live = {"f": {"enabled": True, "value": ""}, "g": {"enabled": True, "value": 10}, "h": {"enabled": True, "value": "true"}}
    assert compare_predictions(pred, ident, lambda i: live).ok
    live["g"]["value"] = 11
    assert not compare_predictions(pred, ident, lambda i: live).ok


def test_missing_live_feature_counts_as_off(identities):
    ident = identities[:1]
    pred = {ident[0].identifier: {"f": {"enabled": False, "value": None}}}
    assert compare_predictions(pred, ident, lambda i: {}).ok
    pred = {ident[0].identifier: {"f": {"enabled": True, "value": None}}}
    assert not compare_predictions(pred, ident, lambda i: {}).ok


def _applied_with_other_segment_id(env_doc, plan, res, real_id):
    """What the live server would look like if it had assigned `real_id` to the new segment."""
    live = copy.deepcopy(res.after_document)
    for s in live["project"]["segments"]:
        if s["name"] == "pro_users_10pct":
            s["id"] = real_id
    return live


def test_server_assigned_segment_id_is_diagnosed_and_reprediction_matches(env_doc, plan, identities):
    res = compute_impact(env_doc, plan, identities)
    assert res.report["predicted_ids"]["segments"] == {"pro_users_10pct": 3}
    # try ids until the percentage split lands differently (the split hashes on the segment id)
    for real_id in range(4, 60):
        live = _applied_with_other_segment_id(env_doc, plan, res, real_id)
        out = verify_prediction(res, sdk_offline_eval(live), live_document=live)
        if not out.ok:
            break
    else:  # pragma: no cover
        raise AssertionError("no id changed the split membership")
    assert any(f"got id {real_id} but the preview assumed 3" in d for d in out.diagnosis)
    assert any("flag engine on the live environment document agrees" in d and "differently than predicted" in d
               for d in out.diagnosis)
    assert out.reprediction is not None and out.reprediction.ok
    assert "with server-assigned ids the preview would have been: prediction matched 200/200" in out.render()
    assert hints_from_live_document(res, live) == {"segment_ids": {"pro_users_10pct": real_id}}


def test_missing_segment_in_live_document_is_diagnosed(env_doc, plan, identities):
    res = compute_impact(env_doc, plan, identities)
    out = verify_prediction(res, sdk_offline_eval(env_doc), live_document=env_doc)
    assert any("not present in the live environment document" in d for d in out.diagnosis)


def test_repredict_with_live_ids_is_exact_and_drops_the_assumption(env_doc, plan, identities):
    res = compute_impact(env_doc, plan, identities)
    live = _applied_with_other_segment_id(env_doc, plan, res, 31)
    real = repredict_with_live_ids(res, live)
    assert real.report["predicted_ids"]["segments"] == {"pro_users_10pct": 31}
    assert real.report["assumptions"] == [] and real.report["id_sensitivity"] is None
    out = verify_prediction(real, sdk_offline_eval(live))
    assert out.ok
