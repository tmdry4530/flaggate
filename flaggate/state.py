"""Environment snapshot, base_state_hash, and pure plan simulation.

A Snapshot is the semantic state of one Flagsmith project + environment that a
plan can touch: features (type, variants, env default, segment overrides,
archived) and segments (rules). ``state_hash`` is the ``base_state_hash`` the
plan is bound to. ``simulate`` applies plan ops to a copy of a snapshot without
touching Flagsmith; it is used for validation, diffs and rollback generation
(and is available to the impact engine).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from pydantic import BaseModel, Field

from flaggate.plan import (
    AddVariantOp,
    ArchiveFeatureOp,
    DeleteFeatureOp,
    CreateFeatureOp,
    CreateMvFeatureOp,
    CreateSegmentOp,
    DeleteSegmentOp,
    DeleteSegmentOverrideOp,
    Op,
    SegmentRule,
    SetFlagOp,
    SetMvWeightsOp,
    SetSegmentOverrideOp,
    Target,
    UpdateSegmentRulesOp,
)

FlagValue = str | int | bool | None
EPS = 1e-6


class PlanConflict(ValueError):
    """The plan cannot be applied to this state (unknown feature, bad weights, ...)."""


class VariantView(BaseModel):
    id: int | None = None
    default_weight: float = 0.0


class EnvState(BaseModel):
    enabled: bool = False
    value: FlagValue = None
    weights: dict[str, float] = Field(default_factory=dict)  # MV only; includes the control value


class OverrideView(BaseModel):
    segment_id: int | None = None
    priority: int = 0
    enabled: bool = False
    value: FlagValue = None
    weights: dict[str, float] | None = None  # MV only; includes the override's control value


class FeatureView(BaseModel):
    id: int | None = None
    type: str = "STANDARD"
    archived: bool = False
    initial_value: FlagValue = None
    description: str | None = None
    variants: dict[str, VariantView] = Field(default_factory=dict)
    env: EnvState = Field(default_factory=EnvState)
    overrides: dict[str, OverrideView] = Field(default_factory=dict)


class SegmentView(BaseModel):
    id: int | None = None
    rules: list[dict[str, Any]] = Field(default_factory=list)


class Snapshot(BaseModel):
    target: Target
    environment_id: int | None = None
    features: dict[str, FeatureView] = Field(default_factory=dict)
    segments: dict[str, SegmentView] = Field(default_factory=dict)


# --------------------------------------------------------------------------- hashing
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def state_hash(snapshot: Snapshot) -> str:
    """``sha256:<hex>`` over the canonical JSON of the whole snapshot."""
    return "sha256:" + sha256_hex(canonical(snapshot.model_dump(mode="json")))


def env_fingerprint(api_url: str, env_key: str) -> str:
    """Non-secret fingerprint binding a plan to one environment of one instance."""
    return "sha256:" + sha256_hex(f"{api_url.rstrip('/')}|{env_key}")


# --------------------------------------------------------------------------- values / rules
def value_key(value: FlagValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def normalize_rules(rules: Iterable[Any]) -> list[dict[str, Any]]:
    """Plan SegmentRule models or Flagsmith API rule dicts -> canonical dicts."""
    out = []
    for r in rules:
        if isinstance(r, SegmentRule):
            r = r.model_dump()
        out.append(
            {
                "type": r["type"],
                "rules": normalize_rules(r.get("rules") or []),
                "conditions": [
                    {
                        "property": c.get("property") or "",
                        "operator": c["operator"],
                        "value": "" if c.get("value") is None else str(c.get("value")),
                    }
                    for c in (r.get("conditions") or [])
                ],
            }
        )
    return out


def rules_to_api(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical rule dicts -> Flagsmith API payload (empty property -> null, required by PERCENTAGE_SPLIT)."""
    return [
        {
            "type": r["type"],
            "rules": rules_to_api(r["rules"]),
            "conditions": [
                {"property": c["property"] or None, "operator": c["operator"], "value": c["value"]}
                for c in r["conditions"]
            ],
        }
        for r in rules
    ]


def describe_rules(rules: list[dict[str, Any]]) -> str:
    def cond(c: dict[str, Any]) -> str:
        if c["operator"] == "PERCENTAGE_SPLIT":
            return f"{c['value']}% split"
        op = {"EQUAL": "==", "NOT_EQUAL": "!=", "GREATER_THAN": ">", "LESS_THAN": "<"}.get(c["operator"], c["operator"])
        return f"{c['property']} {op} {c['value']}"

    def rule(r: dict[str, Any]) -> str:
        joiner = {"ALL": " AND ", "ANY": " OR ", "NONE": " NOR "}[r["type"]]
        parts = [cond(c) for c in r["conditions"]] + [f"({rule(x)})" for x in r["rules"]]
        return joiner.join(parts) if parts else "true"

    return " AND ".join(rule(r) for r in rules) or "true"


# --------------------------------------------------------------------------- MV helpers
def control_key(feature: FeatureView) -> str:
    v = feature.env.value if feature.env.value is not None else feature.initial_value
    return value_key(v)


def full_weights(variant_weights: dict[str, float], control: str) -> dict[str, float]:
    total = sum(variant_weights.values())
    out = {k: round(float(w), 6) for k, w in variant_weights.items()}
    out[control] = round(out.get(control, 0.0) + 100.0 - total, 6)
    return out


def check_weights(feature_name: str, feature: FeatureView, weights: dict[str, float], control: str) -> None:
    expected = set(feature.variants) | {control}
    got = set(weights)
    if got != expected:
        missing, extra = sorted(expected - got), sorted(got - expected)
        raise PlanConflict(
            f"{feature_name}: weights must name every variant plus the control {control!r}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unknown {extra}" if extra else "")
        )


# --------------------------------------------------------------------------- resolution
class Refs:
    """Maps plan-local ``@ref`` names to real segment/feature names."""

    def __init__(self) -> None:
        self.segments: dict[str, str] = {}
        self.features: dict[str, str] = {}

    def segment(self, state: Snapshot, ref: str) -> str:
        if ref.startswith("@"):
            if ref[1:] not in self.segments:
                raise PlanConflict(f"unknown segment ref {ref}")
            return self.segments[ref[1:]]
        if ref.isdigit():
            for name, s in state.segments.items():
                if s.id == int(ref):
                    return name
            raise PlanConflict(f"unknown segment id {ref}")
        if ref not in state.segments:
            raise PlanConflict(f"unknown segment {ref!r}")
        return ref

    def feature(self, state: Snapshot, ref: str) -> str:
        if ref.startswith("@"):
            if ref[1:] not in self.features:
                raise PlanConflict(f"unknown feature ref {ref}")
            return self.features[ref[1:]]
        if ref not in state.features:
            raise PlanConflict(f"unknown feature {ref!r}")
        return ref


# --------------------------------------------------------------------------- simulation
def apply_op(state: Snapshot, op: Op, refs: Refs) -> None:
    """Apply one op to ``state`` in place (no Flagsmith calls). Raises PlanConflict."""
    if isinstance(op, (CreateFeatureOp, CreateMvFeatureOp)):
        if op.name in state.features:
            raise PlanConflict(f"feature {op.name!r} already exists")
        if isinstance(op, CreateFeatureOp):
            fv = FeatureView(type="STANDARD", initial_value=op.initial_value, description=op.description,
                             env=EnvState(enabled=op.default_enabled, value=op.initial_value))
        else:
            control = op.initial_value
            fv = FeatureView(
                type="MULTIVARIATE", initial_value=control, description=op.description,
                variants={k: VariantView(default_weight=w) for k, w in op.weights.items() if k != control},
                env=EnvState(enabled=op.default_enabled, value=control, weights=dict(op.weights)),
            )
        state.features[op.name] = fv
        if op.ref:
            refs.features[op.ref] = op.name
    elif isinstance(op, AddVariantOp):
        name = refs.feature(state, op.feature)
        f = state.features[name]
        if f.type != "MULTIVARIATE":
            raise PlanConflict(f"{name}: add_variant needs a MULTIVARIATE feature")
        if op.value in f.variants or op.value == control_key(f):
            raise PlanConflict(f"{name}: variant {op.value!r} already exists")
        ck = control_key(f)
        f.variants[op.value] = VariantView(default_weight=op.weight)
        w = dict(f.env.weights)
        w[op.value] = op.weight
        w[ck] = round(w.get(ck, 0.0) - op.weight, 6)
        if w[ck] < -EPS:
            raise PlanConflict(f"{name}: adding {op.value!r} at {op.weight}% leaves the control below 0%")
        f.env.weights = w
    elif isinstance(op, SetFlagOp):
        name = refs.feature(state, op.feature)
        f = state.features[name]
        if f.type == "MULTIVARIATE":
            if op.value is None:
                raise PlanConflict(f"{name}: a MULTIVARIATE flag needs a control value")
            old, new = control_key(f), value_key(op.value)
            if new != old:
                if new in f.variants:
                    raise PlanConflict(f"{name}: control value {new!r} collides with a variant")
                w = dict(f.env.weights)
                w[new] = w.pop(old, 0.0)
                f.env.weights = w
        f.env.enabled, f.env.value = op.enabled, op.value
    elif isinstance(op, SetMvWeightsOp):
        name = refs.feature(state, op.feature)
        f = state.features[name]
        if f.type != "MULTIVARIATE":
            raise PlanConflict(f"{name}: set_mv_weights needs a MULTIVARIATE feature")
        check_weights(name, f, op.weights, control_key(f))
        f.env.weights = {k: float(v) for k, v in op.weights.items()}
    elif isinstance(op, CreateSegmentOp):
        if op.name in state.segments:
            raise PlanConflict(f"segment {op.name!r} already exists")
        state.segments[op.name] = SegmentView(rules=normalize_rules(op.rules))
        if op.ref:
            refs.segments[op.ref] = op.name
    elif isinstance(op, UpdateSegmentRulesOp):
        name = refs.segment(state, op.segment)
        state.segments[name].rules = normalize_rules(op.rules)
    elif isinstance(op, SetSegmentOverrideOp):
        fname = refs.feature(state, op.feature)
        sname = refs.segment(state, op.segment)
        f = state.features[fname]
        weights = None
        if op.weights is not None:
            if f.type != "MULTIVARIATE":
                raise PlanConflict(f"{fname}: override weights need a MULTIVARIATE feature")
            check_weights(fname, f, op.weights, value_key(op.value))
            weights = {k: float(v) for k, v in op.weights.items()}
        prev = f.overrides.get(sname)
        if weights is None and prev is not None and f.type == "MULTIVARIATE":
            weights = prev.weights
        f.overrides[sname] = OverrideView(segment_id=state.segments[sname].id, priority=op.priority,
                                          enabled=op.enabled, value=op.value, weights=weights)
    elif isinstance(op, DeleteSegmentOverrideOp):
        fname = refs.feature(state, op.feature)
        sname = refs.segment(state, op.segment)
        if sname not in state.features[fname].overrides:
            raise PlanConflict(f"{fname} has no override for segment {sname!r}")
        del state.features[fname].overrides[sname]
    elif isinstance(op, DeleteSegmentOp):
        sname = refs.segment(state, op.segment)
        del state.segments[sname]
        for f in state.features.values():
            f.overrides.pop(sname, None)
    elif isinstance(op, ArchiveFeatureOp):
        name = refs.feature(state, op.feature)
        if state.features[name].archived:
            raise PlanConflict(f"feature {name!r} is already archived")
        state.features[name].archived = True
    elif isinstance(op, DeleteFeatureOp):
        name = refs.feature(state, op.feature)
        del state.features[name]
    else:  # pragma: no cover - the Op union is closed
        raise PlanConflict(f"unsupported op {getattr(op, 'op', op)!r}")


def simulate(snapshot: Snapshot, ops: Iterable[Op]) -> Snapshot:
    """Return a new snapshot with ``ops`` applied in order. Raises PlanConflict."""
    state = snapshot.model_copy(deep=True)
    refs = Refs()
    for i, op in enumerate(ops):
        try:
            apply_op(state, op, refs)
        except PlanConflict as e:
            raise PlanConflict(f"op #{i} ({op.op}): {e}") from None
    return state


# --------------------------------------------------------------------------- live snapshot
def _from_api_value(v: dict[str, Any] | None) -> FlagValue:
    if v is None:
        return None
    t, raw = v.get("type"), v.get("value")
    if t == "integer":
        return int(raw)
    if t == "boolean":
        return str(raw).lower() == "true"
    return raw


def _option_key(o: dict[str, Any]) -> str:
    t = o.get("type")
    if t == "int":
        return str(o.get("integer_value"))
    if t == "bool":
        return value_key(bool(o.get("boolean_value")))
    return o.get("string_value") or ""


def take_snapshot(client: Any, target: Target, env_key: str) -> Snapshot:
    """Read the live project/environment through the Admin API (v1 + the __future__ flag endpoint)."""
    p = target.project_id
    envo = client.environment(env_key)
    segs = [s for s in client.list_all(f"/api/v1/projects/{p}/segments/") if not s.get("feature")]
    seg_name = {s["id"]: s["name"] for s in segs}
    snap = Snapshot(
        target=target,
        environment_id=envo["id"],
        segments={s["name"]: SegmentView(id=s["id"], rules=normalize_rules(s.get("rules") or [])) for s in segs},
    )
    for f in client.list_all(f"/api/v1/projects/{p}/features/"):
        opts = {o["id"]: _option_key(o) for o in f.get("multivariate_options") or []}
        fv = FeatureView(
            id=f["id"], type=f["type"], archived=bool(f.get("is_archived")),
            initial_value=f.get("initial_value"), description=f.get("description"),
            variants={_option_key(o): VariantView(id=o["id"], default_weight=float(o.get("default_percentage_allocation") or 0))
                      for o in f.get("multivariate_options") or []},
        )
        st = client.get(f"/api/__future__/environments/{env_key}/features/{f['id']}/")
        ed = st["environment_default"]
        fv.env = EnvState(enabled=ed["enabled"], value=_from_api_value(ed.get("value")))
        if fv.type == "MULTIVARIATE":
            fv.env.weights = full_weights({opts[v["id"]]: v["weight"] for v in ed.get("variants") or [] if v["id"] in opts},
                                          control_key(fv))
        for so in st.get("segment_overrides") or []:
            sid = so["segment"]["id"]
            val = _from_api_value(so.get("value"))
            w = None
            if fv.type == "MULTIVARIATE" and so.get("variants"):
                w = full_weights({opts[v["id"]]: v["weight"] for v in so["variants"] if v["id"] in opts}, value_key(val))
            fv.overrides[seg_name.get(sid, f"#{sid}")] = OverrideView(
                segment_id=sid, priority=so.get("priority") or 0, enabled=so["enabled"], value=val, weights=w)
        snap.features[f["name"]] = fv
    return snap
