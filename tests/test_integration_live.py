"""Integration against the local self-hosted Flagsmith (127.0.0.1:8600). Skipped when it is unreachable or the
local credentials are missing. Each test builds its own organisation/project/environment (tests/live_helpers.py)
and deletes it afterwards; the demo seed is never touched. Credentials are read from the env file at runtime and
never printed."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from flaggate import Plan, plan_hash
from flaggate.flagsmith_io import (api_url_from_env, fetch_environment_document, fetch_identities,
                                   load_credentials, server_reachable)
from flaggate.identities import Identity
from flaggate.impact import attach_impact, compute_impact
from flaggate.verify import repredict_with_live_ids, sdk_live_eval, verify_live

from live_helpers import LiveProject

pytestmark = pytest.mark.integration
FIX = Path(__file__).parent / "fixtures"
API = api_url_from_env()
CREDS = load_credentials()

if not server_reachable(API) or not all(k in CREDS for k in ("FS_ADMIN_EMAIL", "FS_ADMIN_PASSWORD")):
    pytest.skip("local Flagsmith not reachable / credentials missing", allow_module_level=True)


def _identities(n=60):
    return [Identity(f"it-user-{i}", {"plan": ("pro" if i % 5 < 2 else "free"), "country": "KR"}) for i in range(n)]


@pytest.fixture(scope="module")
def live():
    lp = LiveProject(API, CREDS["FS_ADMIN_EMAIL"], CREDS["FS_ADMIN_PASSWORD"]).create()
    try:
        yield lp
    finally:
        lp.delete()


def _plan(**guard):
    d = json.loads((FIX / "plan_demo.json").read_text())
    d["guardrails"].update(guard)
    return Plan.model_validate(d)


def test_real_env_document_preview_apply_and_verify(live):
    """preview on the real env document -> apply through the Admin API -> SDK read-back vs prediction."""
    idents = _identities()
    # make the identities exist on the server with their traits (what the identities API would list)
    seed = sdk_live_eval(API, live.env_key)
    for i in idents:
        seed(i)

    before_doc = fetch_environment_document(API, live.server_key)
    plan = _plan()
    res = compute_impact(before_doc, plan, idents, sample_source="live identities (it)")
    assert res.approvable and res.report["evaluated"]
    # the engine's *before* state equals what the live server serves today (engine <-> server agree)
    pre = verify_live(compute_impact(before_doc, _plan_noop(plan), idents), api_url=API, environment_key=live.env_key)
    assert pre.ok, pre.render()

    attach_impact(plan, res)                       # impact lives in plan.impact; hash unchanged
    assert plan_hash(attach_impact(plan, res)) == plan_hash(plan)

    # one-shot apply: the segment id is only known after the server created it, so the preview's guess misses...
    live.apply(plan, only=("create_segment",))
    live_doc = fetch_environment_document(API, live.server_key)
    real = repredict_with_live_ids(res, live_doc)
    assert real.report["predicted_ids"]["segments"] != res.report["predicted_ids"]["segments"]
    assert real.report["id_sensitivity"] is None and real.approvable
    # ...two-phase apply: re-predict with the real id before any behaviour changes, then apply the rest
    live.apply(plan, skip=("create_segment",))
    out = verify_live(real, api_url=API, environment_key=live.env_key, server_key=live.server_key)
    assert out.ok and out.summary() == f"prediction matched {len(idents)}/{len(idents)}", out.render()
    # the original preview is still exact for everything that does not depend on the new id (MV re-weighting)
    old = verify_live(res, api_url=API, environment_key=live.env_key, server_key=live.server_key)
    assert all(m.feature != "checkout_button_color" for m in old.mismatches), old.render()
    if not old.ok:   # a wrong guess is explained and corrected, never silently accepted
        assert old.reprediction is not None and old.reprediction.ok
        assert any("preview assumed" in d for d in old.diagnosis)


def _plan_noop(plan: Plan) -> Plan:
    d = json.loads(plan.model_dump_json(by_alias=True))
    d["ops"] = [{"op": "set_mv_weights", "feature": "checkout_button_color",
                 "weights": {"blue": 34, "green": 33, "orange": 33}}]
    return Plan.model_validate(d)


def test_identities_api_listing_returns_traits(live):
    idents = _identities(5)
    seed = sdk_live_eval(API, live.env_key)
    for i in idents:
        seed(i)
    got = {i.identifier: dict(i.traits) for i in fetch_identities(API, live.admin.key, live.env_key, limit=50)}
    assert {i.identifier: dict(i.traits) for i in idents}.items() <= got.items()


def test_verify_detects_a_plan_that_was_not_applied():
    """Fresh project, plan never applied: the live SDK still serves the old state => mismatch, fail closed."""
    lp = LiveProject(API, CREDS["FS_ADMIN_EMAIL"], CREDS["FS_ADMIN_PASSWORD"]).create()
    try:
        idents = _identities(40)
        doc = fetch_environment_document(API, lp.server_key)
        plan = _plan()
        # roll the segment out to ALL pro users so that a missing apply is certain to be visible
        d = json.loads(plan.model_dump_json(by_alias=True))
        d["ops"][0]["rules"][0]["conditions"][1]["value"] = "100"
        d["guardrails"]["max_exposure_pct"] = 100
        res = compute_impact(doc, Plan.model_validate(d), idents)
        assert res.report["changed"] > 0
        out = verify_live(res, api_url=API, environment_key=lp.env_key, server_key=lp.server_key)
        assert not out.ok and out.matched < out.total
        assert any("not present in the live environment document" in x for x in out.diagnosis)
    finally:
        lp.delete()
