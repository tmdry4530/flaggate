#!/usr/bin/env python3
"""End-to-end demo of the impact engine against the LOCAL self-hosted Flagsmith (127.0.0.1:8600).

Creates a throw-away organisation/project/environment (never touches the demo seed), then:
  1. seeds 60 identities, 2. prints the impact preview for the golden example plan,
  3. applies the plan via the Admin API in two phases (test helper, not the real executor): segments first, to learn
     the server-assigned ids, re-previews with them, then the overrides/weights,
  4. prints "prediction matched N/N" from the live SDK read-back, 5. deletes the throw-away organisation.

    .venv/bin/python scripts/impact_live_demo.py
Credentials come from the runtime env file (never printed).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from flaggate import Plan, plan_hash  # noqa: E402
from flaggate.flagsmith_io import api_url_from_env, fetch_environment_document, load_credentials  # noqa: E402
from flaggate.identities import Identity  # noqa: E402
from flaggate.impact import attach_impact, compute_impact  # noqa: E402
from flaggate.verify import repredict_with_live_ids, sdk_live_eval, verify_live  # noqa: E402
from live_helpers import LiveProject  # noqa: E402

api, creds = api_url_from_env(), load_credentials()
lp = LiveProject(api, creds["FS_ADMIN_EMAIL"], creds["FS_ADMIN_PASSWORD"]).create()
try:
    idents = [Identity(f"user-{i}", {"plan": "pro" if i % 5 < 2 else "free", "country": "KR"}) for i in range(60)]
    seed = sdk_live_eval(api, lp.env_key)
    for i in idents:
        seed(i)
    plan = Plan.model_validate_json((ROOT / "tests/fixtures/plan_demo.json").read_text())
    res = compute_impact(fetch_environment_document(api, lp.server_key), plan, idents,
                         sample_source="local Flagsmith identities (throw-away env)")
    plan = attach_impact(plan, res)
    print(res.render(), "\n")
    print(f"plan_hash (impact attached, unchanged): {plan_hash(plan)}\n")
    if not res.approvable:
        sys.exit("not approvable: not applying")
    print("-- human approves; phase 1: create the segment (no behaviour change yet) --\n")
    lp.apply(plan, only=("create_segment",))
    live_doc = fetch_environment_document(api, lp.server_key)
    real = repredict_with_live_ids(res, live_doc)
    ids = real.report["predicted_ids"]["segments"]
    print(f"server assigned segment id(s) {ids} (preview guessed {res.report['predicted_ids']['segments']})")
    print(f"re-preview with the real id: new_checkout exposure {real.report['features']['new_checkout']['exposure_pct']:g}% "
          f"(was {res.report['features']['new_checkout']['exposure_pct']:g}%), approvable={real.approvable}\n")
    if not real.approvable:
        sys.exit("real ids break the guardrail: stopping before any override is applied")
    print("-- phase 2: overrides + MV weights --\n")
    lp.apply(plan, skip=("create_segment",))
    out = verify_live(real, api_url=api, environment_key=lp.env_key, server_key=lp.server_key)
    print("POST-APPLY VERIFY")
    print(out.render())
finally:
    lp.delete()
