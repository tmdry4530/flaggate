from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from flaggate import (
    NON_SEMANTIC_FIELDS,
    SCHEMA,
    Plan,
    SetMvWeightsOp,
    canonical_bytes,
    canonical_json,
    plan_hash,
)

# Minimal complete plan used across tests. Weights include the control and sum to 100.
EXAMPLE = {
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
        "env_fp": "sha256:9c1e0de7a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbcc",
    },
    "base_state_hash": "sha256:41d0bbbbccccddddeeeeffff0000111122223333444455556666777788889999",
    "ops": [
        {
            "op": "create_segment",
            "ref": "seg_pro_10",
            "name": "pro_users_10pct",
            "description": None,
            "rules": [
                {
                    "type": "ALL",
                    "rules": [],
                    "conditions": [
                        {"property": "plan", "operator": "EQUAL", "value": "pro"},
                        {"property": "", "operator": "PERCENTAGE_SPLIT", "value": "10"},
                    ],
                }
            ],
        },
        {
            "op": "set_segment_override",
            "feature": "new_checkout",
            "segment": "@seg_pro_10",
            "enabled": True,
            "value": None,
            "priority": 0,
            "weights": None,
        },
        {
            "op": "set_mv_weights",
            "feature": "checkout_button_color",
            "weights": {"blue": 0, "green": 50, "orange": 50},
        },
    ],
    "rollback": {
        "ops": [
            {"op": "delete_segment_override", "feature": "new_checkout", "segment": "@seg_pro_10"},
            {"op": "delete_segment", "segment": "@seg_pro_10"},
            {
                "op": "set_mv_weights",
                "feature": "checkout_button_color",
                "weights": {"blue": 34, "green": 33, "orange": 33},
            },
        ]
    },
    "guardrails": {"max_exposure_pct": 20, "token_ttl_min": 15},
    "expires_at": "2026-11-28T14:17:11+09:00",
}

# Frozen so a accidental change to canonicalization is a failing test, not a silent drift.
GOLDEN_HASH = "78dd43e38a6d86fe7a598facf69cc0ad1a8d4fb24909d851227ef3e487e35adf"


def test_schema_constant():
    assert SCHEMA == "flaggate.plan/v1"
    assert NON_SEMANTIC_FIELDS == frozenset({"plan_hash", "impact", "status", "metadata"})


def test_round_trip_dict_and_json():
    plan = Plan.model_validate(EXAMPLE)
    as_dict = plan.model_dump(mode="json")
    again = Plan.model_validate(as_dict)
    assert again == plan
    as_json = plan.model_dump_json()
    from_json = Plan.model_validate_json(as_json)
    assert from_json == plan
    assert plan_hash(from_json) == plan_hash(plan)
    assert from_json.schema_name == SCHEMA
    assert from_json.ops[2].weights["green"] == 50


def test_unknown_op_rejected():
    data = deepcopy(EXAMPLE)
    data["ops"] = [{"op": "frobnicate", "feature": "new_checkout"}]
    with pytest.raises(ValidationError) as exc:
        Plan.model_validate(data)
    assert "frobnicate" in str(exc.value) or "union" in str(exc.value).lower() or "discriminator" in str(exc.value).lower()


def test_unknown_field_rejected():
    data = deepcopy(EXAMPLE)
    data["not_a_field"] = "nope"
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


def test_weights_must_sum_to_100():
    data = deepcopy(EXAMPLE)
    data["ops"] = [
        {"op": "set_mv_weights", "feature": "checkout_button_color", "weights": {"green": 50, "orange": 40}}
    ]
    with pytest.raises(ValidationError, match="sum to 100"):
        Plan.model_validate(data)
    with pytest.raises(ValidationError, match="sum to 100"):
        SetMvWeightsOp(op="set_mv_weights", feature="x", weights={"green": 33, "orange": 33})


def test_create_mv_feature_control_and_weights():
    data = deepcopy(EXAMPLE)
    data["ops"] = [
        {
            "op": "create_mv_feature",
            "name": "checkout_button_color",
            "default_enabled": True,
            "initial_value": "blue",
            "weights": {"blue": 34, "green": 33, "orange": 33},
            "description": None,
            "ref": None,
        }
    ]
    plan = Plan.model_validate(data)
    assert plan.ops[0].initial_value == "blue"
    bad = deepcopy(data)
    bad["ops"][0]["initial_value"] = "purple"
    with pytest.raises(ValidationError, match="initial_value"):
        Plan.model_validate(bad)


def test_naive_datetime_rejected():
    data = deepcopy(EXAMPLE)
    data["created_at"] = "2026-11-28T14:02:11"
    with pytest.raises(ValidationError, match="timezone-aware"):
        Plan.model_validate(data)


def test_hash_stable_under_key_order_changes():
    plan_a = Plan.model_validate(EXAMPLE)

    shuffled = {
        "expires_at": EXAMPLE["expires_at"],
        "rollback": {
            "ops": [
                {"segment": "@seg_pro_10", "feature": "new_checkout", "op": "delete_segment_override"},
                {"segment": "@seg_pro_10", "op": "delete_segment"},
                {
                    "weights": {"orange": 33, "blue": 34, "green": 33},
                    "feature": "checkout_button_color",
                    "op": "set_mv_weights",
                },
            ]
        },
        "prompt": EXAMPLE["prompt"],
        "plan_id": EXAMPLE["plan_id"],
        "schema": EXAMPLE["schema"],
        "author": EXAMPLE["author"],
        "created_at": EXAMPLE["created_at"],
        "guardrails": {"token_ttl_min": 15, "max_exposure_pct": 20},
        "base_state_hash": EXAMPLE["base_state_hash"],
        "target": {
            "env_fp": EXAMPLE["target"]["env_fp"],
            "environment": "Development",
            "project_id": 1,
            "organisation_id": 1,
            "api_url": "http://127.0.0.1:8600",
        },
        "ops": [
            {
                "name": "pro_users_10pct",
                "rules": [
                    {
                        "conditions": [
                            {"value": "pro", "operator": "EQUAL", "property": "plan"},
                            {"value": "10", "operator": "PERCENTAGE_SPLIT", "property": ""},
                        ],
                        "rules": [],
                        "type": "ALL",
                    }
                ],
                "description": None,
                "ref": "seg_pro_10",
                "op": "create_segment",
            },
            {
                "weights": None,
                "priority": 0,
                "value": None,
                "enabled": True,
                "segment": "@seg_pro_10",
                "feature": "new_checkout",
                "op": "set_segment_override",
            },
            {
                "weights": {"orange": 50, "green": 50, "blue": 0},
                "feature": "checkout_button_color",
                "op": "set_mv_weights",
            },
        ],
    }
    plan_b = Plan.model_validate(shuffled)
    assert canonical_bytes(plan_a) == canonical_bytes(plan_b)
    assert plan_hash(plan_a) == plan_hash(plan_b)

    # Same payload as a JSON object with keys in a different textual order.
    text = json.dumps(shuffled, separators=(",", ":"))
    plan_c = Plan.model_validate_json(text)
    assert plan_hash(plan_c) == plan_hash(plan_a)


def test_hash_changes_on_semantic_change():
    plan = Plan.model_validate(EXAMPLE)
    baseline = plan_hash(plan)

    changed_weight = deepcopy(EXAMPLE)
    changed_weight["ops"][2]["weights"] = {"blue": 0, "green": 49, "orange": 51}
    assert plan_hash(Plan.model_validate(changed_weight)) != baseline

    changed_target = deepcopy(EXAMPLE)
    changed_target["target"]["environment"] = "Production"
    assert plan_hash(Plan.model_validate(changed_target)) != baseline

    changed_ops_order = deepcopy(EXAMPLE)
    changed_ops_order["ops"][0], changed_ops_order["ops"][1] = (
        changed_ops_order["ops"][1],
        changed_ops_order["ops"][0],
    )
    assert plan_hash(Plan.model_validate(changed_ops_order)) != baseline


def test_hash_ignores_non_semantic_fields():
    plan = Plan.model_validate(EXAMPLE)
    decorated = plan.model_copy(
        update={
            "status": "approved",
            "impact": {"n": 200, "max_exposure_pct": 11.5},
            "plan_hash": "deadbeef",
            "metadata": {"note": "harmless annotation", "ticket": "TW-1"},
        }
    )
    assert plan_hash(decorated) == plan_hash(plan)
    for field in NON_SEMANTIC_FIELDS:
        assert field not in canonical_json(plan)
        assert field not in canonical_json(decorated)


def test_canonical_json_sorted_keys_no_whitespace():
    text = canonical_json(Plan.model_validate(EXAMPLE))
    assert "\n" not in text and "\t" not in text
    parsed = json.loads(text)
    # no insignificant whitespace; object keys sorted at every level
    assert text == json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert list(parsed.keys()) == sorted(parsed.keys())
    assert list(parsed["target"].keys()) == sorted(parsed["target"].keys())
    assert list(parsed["ops"][2]["weights"].keys()) == ["blue", "green", "orange"]
    # created_at / expires_at are folded to UTC second-precision Z
    assert parsed["created_at"] == "2026-11-28T05:02:11Z"
    assert parsed["expires_at"] == "2026-11-28T05:17:11Z"
    # 14:02 +09 and the same instant written as UTC hash equal
    utc_twin = deepcopy(EXAMPLE)
    utc_twin["created_at"] = "2026-11-28T05:02:11Z"
    utc_twin["expires_at"] = "2026-11-28T05:17:11+00:00"
    assert plan_hash(Plan.model_validate(utc_twin)) == plan_hash(Plan.model_validate(EXAMPLE))


def test_plan_hash_is_sha256_hex():
    digest = plan_hash(Plan.model_validate(EXAMPLE))
    assert len(digest) == 64
    int(digest, 16)  # noqa: B018 — valid hex
    assert digest == digest.lower()
    assert not digest.startswith("sha256:")


def test_golden_hash():
    digest = plan_hash(Plan.model_validate(EXAMPLE))
    # Imported late so the test file can be used to print the digest on first run.
    assert digest == GOLDEN_HASH


def test_empty_ops_rejected():
    data = deepcopy(EXAMPLE)
    data["ops"] = []
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


def test_aware_datetime_objects_accepted():
    plan = Plan.model_validate(EXAMPLE)
    rebuilt = plan.model_copy(
        update={"created_at": datetime(2026, 11, 28, 5, 2, 11, tzinfo=timezone.utc)}
    )
    assert plan_hash(rebuilt) == plan_hash(plan)


def test_max_variant_shift_pct_none_keeps_golden_hash():
    plan = Plan.model_validate(EXAMPLE)
    assert plan.guardrails.max_variant_shift_pct is None
    assert "max_variant_shift_pct" not in canonical_json(plan)
    assert plan_hash(plan) == GOLDEN_HASH == "78dd43e38a6d86fe7a598facf69cc0ad1a8d4fb24909d851227ef3e487e35adf"
    explicit_null = deepcopy(EXAMPLE)
    explicit_null["guardrails"]["max_variant_shift_pct"] = None
    assert plan_hash(Plan.model_validate(explicit_null)) == GOLDEN_HASH


def test_max_variant_shift_pct_set_is_semantic_and_validated():
    data = deepcopy(EXAMPLE)
    data["guardrails"]["max_variant_shift_pct"] = 60
    plan = Plan.model_validate(data)
    assert '"max_variant_shift_pct":60.0' in canonical_json(plan)
    assert plan_hash(plan) != GOLDEN_HASH
    data["guardrails"]["max_variant_shift_pct"] = 61
    assert plan_hash(Plan.model_validate(data)) != plan_hash(plan)
    for bad in (-1, 101, float("nan")):
        data["guardrails"]["max_variant_shift_pct"] = bad
        with pytest.raises(ValidationError):
            Plan.model_validate(data)
