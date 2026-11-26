# FlagGate plan schema (`flaggate.plan/v1`)

Normative interface for the impact engine (개발팀3, DESIGN §5.3) and later executor/gatekeeper work.
The module is self-contained: no Flagsmith client.

Install / import (Python ≥ 3.11):

```bash
# from this repo, after `uv sync --extra dev` (venv is gitignored)
from flaggate import Plan, Op, plan_hash, canonical_bytes, canonical_json
from flaggate.plan import SetMvWeightsOp, SegmentRule  # any model below
```

## Public API

| Name | Kind | Signature / role |
|---|---|---|
| `SCHEMA` | const | `"flaggate.plan/v1"` |
| `NON_SEMANTIC_FIELDS` | const | `frozenset({"plan_hash", "impact", "status", "metadata"})` |
| `Plan` | model | the plan document |
| `Target` | model | `api_url: str`, `organisation_id: int`, `project_id: int`, `environment: str` (name), `env_fp: str` |
| `Guardrails` | model | `max_exposure_pct: float` (0–100), `token_ttl_min: int` (≥ 1, default 15), `max_variant_shift_pct: float \| None` (0–100, default `None`; added 2026-11-26, EL-approved) |
| `Rollback` | model | `ops: list[Op]` (may be empty; gatekeeper overwrites these later) |
| `SegmentRule` | model | `type: "ALL"\|"ANY"\|"NONE"`, `rules: list[SegmentRule]`, `conditions: list[SegmentCondition]` |
| `SegmentCondition` | model | `property: str`, `operator: str`, `value: str` |
| `Op` | union | discriminated on `op` (see below) |
| `plan_hash(plan)` | fn | `def plan_hash(plan: Plan) -> str` — SHA-256 hex of `canonical_bytes(plan)` (64 lowercase hex chars, **no** `sha256:` prefix) |
| `canonical_bytes(plan)` | fn | `def canonical_bytes(plan: Plan) -> bytes` |
| `canonical_json(plan)` | fn | `def canonical_json(plan: Plan) -> str` — UTF-8 text of `canonical_bytes` |

Parse: `Plan.model_validate(dict)` or `Plan.model_validate_json(text)`. Extra fields are rejected (`extra="forbid"`). Datetimes must be timezone-aware.

Match ops with `op.op == "set_mv_weights"` or `isinstance(op, SetMvWeightsOp)`. Walk `plan.ops` **in order**. A `segment` or `feature` value starting with `@` is a local ref from a earlier `create_*` op's `ref` field; resolving `@ref` is the impact/executor's job, not this module's.

The JSON key `"schema"` is the Pydantic field `Plan.schema_name` (`alias="schema"`).

## `Plan` fields

| Field | In the hash? | Notes |
|---|---|---|
| `schema` | yes | must be `"flaggate.plan/v1"` |
| `plan_id` | yes | `[A-Za-z0-9._-]+` |
| `created_at` | yes | timezone-aware datetime |
| `author` | yes | |
| `prompt` | yes | original request text |
| `target` | yes | binds the plan to one self-hosted instance + project + environment |
| `base_state_hash` | yes | snapshot hash of Flagsmith state before this plan; opaque string |
| `ops` | yes | non-empty, **ordered** v1 operations |
| `rollback` | yes | inverse ops (same `Op` union) |
| `guardrails` | yes | |
| `expires_at` | yes | optional timezone-aware expiry of the plan itself |
| `metadata` | **no** | `dict[str, str]` free-form notes |
| `plan_hash` | **no** | optional stored digest; recompute with `plan_hash()`, do not trust the field |
| `impact` | **no** | optional dict filled later by the impact engine (DESIGN §5.3) |
| `status` | **no** | optional: `draft` `submitted` `approved` `refused` `applied` `apply_failed` `rolled_back` `promoted` `expired` |

## v1 ops

All ops are strict models (unknown fields and unknown `op` values fail validation).

| `op` | Class | Fields |
|---|---|---|
| `create_feature` | `CreateFeatureOp` | `name`, `type="STANDARD"`, `default_enabled=False`, `initial_value` (`str\|int\|bool\|None`), `description?`, `ref?` |
| `create_mv_feature` | `CreateMvFeatureOp` | `name`, `default_enabled=True`, `initial_value` (control **value**, must be a key of `weights`), `weights` (value → percent, **must sum to 100**), `description?`, `ref?` |
| `add_variant` | `AddVariantOp` | `feature`, `value`, `weight` (0–100; no sum check here), `key?` |
| `set_flag` | `SetFlagOp` | `feature`, `enabled`, `value` — environment **default** state (enable/disable + value), not a segment override |
| `set_mv_weights` | `SetMvWeightsOp` | `feature`, `weights` (value → percent, **must include every variant including the control and sum to 100**) |
| `create_segment` | `CreateSegmentOp` | `name`, `rules`, `description?`, `ref?` |
| `update_segment_rules` | `UpdateSegmentRulesOp` | `segment` (name, numeric id, or `@ref`), `rules` |
| `set_segment_override` | `SetSegmentOverrideOp` | `feature`, `segment`, `enabled`, `value`, `priority=0`, `weights?` (if set, must sum to 100) |
| `delete_segment_override` | `DeleteSegmentOverrideOp` | `feature`, `segment` |
| `delete_segment` | `DeleteSegmentOp` | `segment` |
| `archive_feature` | `ArchiveFeatureOp` | `feature` |
| `delete_feature` | `DeleteFeatureOp` | `feature` — **rollback-only**: valid only to undo a `create_feature`/`create_mv_feature` of the same forward plan. The gatekeeper refuses it in a submitted (forward) plan and for any feature that existed before that plan. |

Not in v1 (deferred): `unarchive_feature`, `delete_variant`. Their effects stay non-reversible and are listed as such on
the approval screen (`archive_feature` cannot be undone; an added variant is rolled back to weight 0).

Weights are validated with an absolute tolerance of `1e-6`. `33+33=66` is rejected; include the control, e.g. `{"blue": 34, "green": 33, "orange": 33}`.

## Canonicalization and `plan_hash`

```text
plan_hash(plan) = hex(sha256(canonical_bytes(plan)))
```

`canonical_bytes` is UTF-8 JSON of the plan after dropping `NON_SEMANTIC_FIELDS`, then:

1. Object keys sorted lexicographically at every level (`json.dumps(..., sort_keys=True)`).
2. No insignificant whitespace (`separators=(",", ":")`). Spaces inside string values are kept.
3. List order is preserved (`ops` are ordered; swapping two ops changes the hash).
4. Datetimes are converted to UTC and formatted as `YYYY-MM-DDTHH:MM:SSZ` (second precision, trailing `Z`). `2026-11-28T14:02:11+09:00` and `2026-11-28T05:02:11Z` are the same.
5. Floats are JSON numbers using Python's default encoder; `-0.0` is collapsed to `0.0`; non-finite values are rejected. Integer-valued floats stay floats (`50` → `50.0`).
6. Enums are replaced by their values. `None` is emitted as `null` (not omitted), so an explicit null is visible in the hash payload. **Exception:** `guardrails.max_variant_shift_pct` is omitted when `None`, so plans written before the field existed keep their hash (the golden hash below is locked by a test).
7. `ensure_ascii=False` (UTF-8).

Changing a semantic field, including key order in the *input*, must not change the hash unless the *value* changed. Changing `status`, `impact`, `plan_hash`, or `metadata` must not change the hash.

## Guardrail semantics

* `max_exposure_pct`: max share of sampled identities whose enabled state or plain value changes (per feature).
* `max_variant_shift_pct`: optional cap on multivariate variant re-assignment (A/B weight changes), reported separately as "variant shift". `None` = not enforced.

## Example

The object below is the fixture in `tests/test_plan.py`. Its `plan_hash(...)` is:

```
78dd43e38a6d86fe7a598facf69cc0ad1a8d4fb24909d851227ef3e487e35adf
```

```json
{
  "schema": "flaggate.plan/v1",
  "plan_id": "pl_20261128_7f3a",
  "created_at": "2026-11-28T14:02:11+09:00",
  "author": "planner:rules",
  "prompt": "Roll out new_checkout to 10% of pro users; A/B test checkout_button_color green vs orange 50/50",
  "target": {
    "api_url": "http://127.0.0.1:8600",
    "organisation_id": 1,
    "project_id": 1,
    "environment": "Development",
    "env_fp": "sha256:9c1e0de7a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbcc"
  },
  "base_state_hash": "sha256:41d0bbbbccccddddeeeeffff0000111122223333444455556666777788889999",
  "ops": [
    {
      "op": "create_segment",
      "ref": "seg_pro_10",
      "name": "pro_users_10pct",
      "description": null,
      "rules": [
        {
          "type": "ALL",
          "rules": [],
          "conditions": [
            {"property": "plan", "operator": "EQUAL", "value": "pro"},
            {"property": "", "operator": "PERCENTAGE_SPLIT", "value": "10"}
          ]
        }
      ]
    },
    {
      "op": "set_segment_override",
      "feature": "new_checkout",
      "segment": "@seg_pro_10",
      "enabled": true,
      "value": null,
      "priority": 0,
      "weights": null
    },
    {
      "op": "set_mv_weights",
      "feature": "checkout_button_color",
      "weights": {"blue": 0, "green": 50, "orange": 50}
    }
  ],
  "rollback": {
    "ops": [
      {"op": "delete_segment_override", "feature": "new_checkout", "segment": "@seg_pro_10"},
      {"op": "delete_segment", "segment": "@seg_pro_10"},
      {
        "op": "set_mv_weights",
        "feature": "checkout_button_color",
        "weights": {"blue": 34, "green": 33, "orange": 33}
      }
    ]
  },
  "guardrails": {"max_exposure_pct": 20, "token_ttl_min": 15},
  "expires_at": "2026-11-28T14:17:11+09:00"
}
```
