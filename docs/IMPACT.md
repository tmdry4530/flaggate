# Impact engine and post-apply verification (DESIGN §5.3)

`flaggate/impact.py`, `flaggate/verify.py` (+ `docops.py`, `identities.py`, `flagsmith_io.py`). Input is a
`flaggate.Plan`, a Flagsmith environment document (`/api/v1/environment-document/`) and identities
(identifier + traits). Nothing here writes to Flagsmith except the test helper.

```python
from flaggate import Plan
from flaggate.impact import compute_impact, attach_impact
from flaggate.verify import verify_live

res = compute_impact(env_doc, plan, identities)     # pure; flagsmith-flag-engine before/after
print(res.render())                                  # who flips, per-segment, guardrails, verdict
res.approvable                                       # False on guardrail breach / unappliable plan / empty sample
plan = attach_impact(plan, res)                      # plan.impact = res.report; plan_hash unchanged
...apply...
verify_live(res, api_url=..., environment_key=..., server_key=...).summary()   # "prediction matched 60/60"
```

* **Evaluation**: `flagsmith.mappers.map_environment_document_to_context` + `flag_engine.engine.get_evaluation_result`,
  the same path the SDK's local evaluation uses (tests cross-check against the SDK in offline mode).
* **Ops** are applied to a deep copy of the document (`docops.apply_ops`); `@ref`s (segments and features) resolve
  in op order. An op that cannot be applied stops the preview and the plan is not approvable.
* **Report**: §5.3 shape plus `kind` per flip (`toggle` / `value` / `variant`), `by_segment`, per-feature exposure,
  per-segment membership (`members_before/after`, `entered`, `left`, `flips`), `guardrails`, `blockers`, `warnings`,
  `assumptions`, `id_sensitivity`, `approvable`.
* **Exposure** = share of sampled identities whose *enabled state or plain value* changes, max over features, must be
  ≤ `plan.guardrails.max_exposure_pct`. Variant re-assignment from MV weight changes (A/B) is reported separately
  (`variant_shift_pct`) and only limited if `max_variant_shift_pct=` is passed (it is not in the plan schema).
* **Server-assigned ids**: `PERCENTAGE_SPLIT` hashes on the segment id, a database sequence the preview cannot know
  (verified live: guessed 1, server assigned 12). The preview therefore also evaluates `id_samples` (16) other ids and
  blocks the plan if exposure can exceed the guardrail for any of them. After apply, `verify` reports the plain
  `prediction matched N/N`; if it differs because of the id, it says so, and prints the match of the preview
  re-run with the real id (`hints_from_live_document`).
  **Recommended apply order for the executor (two-phase):** create segments first (inert until an override exists), read the live
  document, `repredict_with_live_ids(res, live_doc)`, re-check `approvable` against the approved numbers, then apply overrides/weights
  and verify against the re-prediction (`scripts/impact_live_demo.py` does exactly this and ends with `prediction matched 60/60`).
  Note the Admin API rejects an empty condition `property` (needs null) for PERCENTAGE_SPLIT; the plan schema uses `""`.
* **Verify** uses the official SDK (`flagsmith==6.3.0`, remote evaluation). Errors count as mismatches (fail closed).
  Non-transient calls store the given traits on the identity; pass `transient=True` to avoid that.

CLI: `python -m flaggate.impact --plan P --env-doc D --identities I.csv` (or `--live --identities-live`),
`python -m flaggate.verify --plan P --before-env-doc D --identities I.csv --live`.
Credentials: `FS_*` env vars or the runtime `local.env` (`FLAGGATE_ENV_FILE` / `FLAGSMITH_RUNTIME`); never printed.

## Tests

`.venv/bin/pytest -q` — offline tests use `tests/fixtures/` (env document, 200 identities, the golden example plan).
`tests/test_integration_live.py` talks to the local self-host (skipped when unreachable). The free plan allows one
project per organisation, so the integration tests and `scripts/impact_live_demo.py` create a throw-away
**organisation** (with project/env/keys) through the local superuser login and delete it afterwards.
