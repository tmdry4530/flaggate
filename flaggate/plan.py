"""FlagGate plan schema (flaggate.plan/v1).

Self-contained: impact and executor code can import Plan, the op types, and
plan_hash without a Flagsmith client. See docs/PLAN_SCHEMA.md.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

SCHEMA = "flaggate.plan/v1"

# Fields that do not change what would be applied. Excluded from canonical
# bytes and therefore from plan_hash().
NON_SEMANTIC_FIELDS = frozenset({"plan_hash", "impact", "status", "metadata"})

# Weights may be specified with floats; this is the allowed absolute error
# around a total of 100.
WEIGHTS_SUM_TOLERANCE = 1e-6

FlagValue = str | int | bool | None
RuleType = Literal["ALL", "ANY", "NONE"]
PlanStatus = Literal[
    "draft",
    "submitted",
    "approved",
    "refused",
    "applied",
    "apply_failed",
    "rolled_back",
    "promoted",
    "expired",
]


def _validate_weights(weights: dict[str, float]) -> dict[str, float]:
    if not weights:
        raise ValueError("weights must not be empty")
    for key, value in weights.items():
        if not key:
            raise ValueError("weight keys must be non-empty variant values")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"weight {key!r} must be a number")
        if not math.isfinite(value) or value < 0 or value > 100:
            raise ValueError(f"weight {key!r}={value} is out of range [0, 100]")
    total = float(sum(weights.values()))
    if not math.isclose(total, 100.0, abs_tol=WEIGHTS_SUM_TOLERANCE, rel_tol=0.0):
        raise ValueError(f"weights must sum to 100, got {total}")
    return weights


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class Target(_Strict):
    """Flagsmith instance and project/environment this plan is bound to."""

    api_url: str = Field(min_length=1)
    organisation_id: int = Field(ge=1)
    project_id: int = Field(ge=1)
    environment: str = Field(min_length=1, description="Environment name, e.g. Development")
    env_fp: str = Field(min_length=1, description="Fingerprint of the environment (opaque string)")


class Guardrails(_Strict):
    max_exposure_pct: float = Field(ge=0, le=100)
    token_ttl_min: int = Field(default=15, ge=1)
    max_variant_shift_pct: float | None = Field(
        default=None, ge=0, le=100,
        description="Optional cap on multivariate variant re-assignment; omitted from the canonical form when None",
    )

    @field_validator("max_exposure_pct", "max_variant_shift_pct")
    @classmethod
    def _finite_exposure(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("guardrail percentages must be finite")
        return value


class SegmentCondition(_Strict):
    property: str = ""
    operator: str = Field(min_length=1, description="Flagsmith operator, e.g. EQUAL, PERCENTAGE_SPLIT")
    value: str = ""


class SegmentRule(_Strict):
    type: RuleType
    rules: list[SegmentRule] = Field(default_factory=list)
    conditions: list[SegmentCondition] = Field(default_factory=list)


class CreateFeatureOp(_Strict):
    op: Literal["create_feature"]
    name: str = Field(min_length=1)
    type: Literal["STANDARD"] = "STANDARD"
    default_enabled: bool = False
    initial_value: FlagValue = None
    description: str | None = None
    ref: str | None = Field(default=None, description="Optional local id, referenced as @ref")


class CreateMvFeatureOp(_Strict):
    """Create a multivariate feature. `weights` maps every variant value (including the control, `initial_value`) to a percentage; they must sum to 100."""

    op: Literal["create_mv_feature"]
    name: str = Field(min_length=1)
    default_enabled: bool = True
    initial_value: str = Field(min_length=1, description="Control variant value; must be a key of weights")
    weights: dict[str, float]
    description: str | None = None
    ref: str | None = None

    @field_validator("weights")
    @classmethod
    def _weights(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_weights(value)

    @model_validator(mode="after")
    def _control_in_weights(self) -> CreateMvFeatureOp:
        if self.initial_value not in self.weights:
            raise ValueError("initial_value must be a key of weights (the control variant)")
        return self


class AddVariantOp(_Strict):
    op: Literal["add_variant"]
    feature: str = Field(min_length=1)
    value: str = Field(min_length=1)
    weight: float = Field(ge=0, le=100)
    key: str | None = None

    @field_validator("weight")
    @classmethod
    def _finite_weight(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("weight must be finite")
        return value


class SetFlagOp(_Strict):
    """Set the environment default enabled bit and/or value (not a segment override)."""

    op: Literal["set_flag"]
    feature: str = Field(min_length=1)
    enabled: bool
    value: FlagValue = None


class SetMvWeightsOp(_Strict):
    """Set multivariate weights for a feature in the current environment. Keys are variant values; values are percentages and must sum to 100."""

    op: Literal["set_mv_weights"]
    feature: str = Field(min_length=1)
    weights: dict[str, float]

    @field_validator("weights")
    @classmethod
    def _weights(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_weights(value)


class CreateSegmentOp(_Strict):
    op: Literal["create_segment"]
    name: str = Field(min_length=1)
    rules: list[SegmentRule]
    description: str | None = None
    ref: str | None = Field(default=None, description="Optional local id, referenced later as @ref")


class UpdateSegmentRulesOp(_Strict):
    op: Literal["update_segment_rules"]
    segment: str = Field(min_length=1, description="Segment name, numeric id, or @ref")
    rules: list[SegmentRule]


class SetSegmentOverrideOp(_Strict):
    op: Literal["set_segment_override"]
    feature: str = Field(min_length=1)
    segment: str = Field(min_length=1, description="Segment name, numeric id, or @ref")
    enabled: bool
    value: FlagValue = None
    priority: int = Field(default=0, ge=0)
    weights: dict[str, float] | None = Field(
        default=None, description="Optional multivariate weights on this override; must sum to 100 when set"
    )

    @field_validator("weights")
    @classmethod
    def _weights(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        return None if value is None else _validate_weights(value)


class DeleteSegmentOverrideOp(_Strict):
    op: Literal["delete_segment_override"]
    feature: str = Field(min_length=1)
    segment: str = Field(min_length=1)


class DeleteSegmentOp(_Strict):
    op: Literal["delete_segment"]
    segment: str = Field(min_length=1)


class ArchiveFeatureOp(_Strict):
    op: Literal["archive_feature"]
    feature: str = Field(min_length=1)


class DeleteFeatureOp(_Strict):
    """Delete a feature. Only valid as the rollback of a feature the same plan created: the gatekeeper refuses it
    in submitted plans and for any feature that existed before the forward plan (see PLAN_SCHEMA.md)."""

    op: Literal["delete_feature"]
    feature: str = Field(min_length=1)


Op = Annotated[
    Union[
        CreateFeatureOp,
        CreateMvFeatureOp,
        AddVariantOp,
        SetFlagOp,
        SetMvWeightsOp,
        CreateSegmentOp,
        UpdateSegmentRulesOp,
        SetSegmentOverrideOp,
        DeleteSegmentOverrideOp,
        DeleteSegmentOp,
        ArchiveFeatureOp,
        DeleteFeatureOp,
    ],
    Field(discriminator="op"),
]

OpAdapter: TypeAdapter[Op] = TypeAdapter(Op)


class Rollback(_Strict):
    ops: list[Op] = Field(default_factory=list)


class Plan(_Strict):
    """A typed, hashable Flagsmith change plan (flaggate.plan/v1)."""

    schema_name: Literal["flaggate.plan/v1"] = Field(default=SCHEMA, alias="schema")
    plan_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    created_at: datetime
    author: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    target: Target
    base_state_hash: str = Field(min_length=1)
    ops: list[Op] = Field(min_length=1)
    rollback: Rollback
    guardrails: Guardrails
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    plan_hash: str | None = None
    impact: dict[str, Any] | None = None
    status: PlanStatus | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value


def _canon_datetime(value: datetime) -> str:
    """UTC ISO-8601 with second precision and a trailing Z."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not allowed in a plan")
        if value == 0:
            return 0.0
        return value
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, datetime):
        return _canon_datetime(value)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return value
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


# (path inside the plan payload) of optional fields added after v1 shipped; omitted from the
# canonical form when None so pre-existing plan hashes stay valid.
_OMIT_WHEN_NONE: tuple[tuple[str, ...], ...] = (("guardrails", "max_variant_shift_pct"),)


def canonical_bytes(plan: Plan) -> bytes:
    """UTF-8 canonical JSON of the semantic fields: sorted keys, no whitespace.

    Datetimes are UTC `YYYY-MM-DDTHH:MM:SSZ`. Floats are JSON numbers (Python's
    default, with `-0.0` collapsed to `0.0`; non-finite values are rejected).
    Enums use their values. `None` is emitted as `null` (not omitted), so an
    explicit null stays distinct from a missing field at parse time. List order
    is preserved (ops are ordered). Exception: optional fields listed in
    `_OMIT_WHEN_NONE` are dropped when None, so adding them did not change
    the hash of existing plans.
    """
    payload = plan.model_dump(mode="python", by_alias=True, exclude=set(NON_SEMANTIC_FIELDS))
    for path in _OMIT_WHEN_NONE:
        node = payload
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(path[-1], ...) is None:
            del node[path[-1]]
    normalized = _normalize(payload)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def canonical_json(plan: Plan) -> str:
    """canonical_bytes decoded as UTF-8."""
    return canonical_bytes(plan).decode("utf-8")


def plan_hash(plan: Plan) -> str:
    """SHA-256 hex digest (64 lowercase hex chars, no algorithm prefix) of canonical_bytes(plan)."""
    return hashlib.sha256(canonical_bytes(plan)).hexdigest()
