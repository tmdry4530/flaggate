"""Apply flaggate.plan/v1 ops to a *copy* of a Flagsmith environment document (pure function, no I/O).

The result is another environment document, so the exact same flag engine evaluation can be run
before and after. This is the "what would Flagsmith serve if this plan were applied" half of the impact
preview. Anything the server decides at apply time (database ids) is *guessed* here; every guess is
reported in ``ApplyResult.assumptions`` so the human sees it and so :mod:`flaggate.verify` can explain
mismatches.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .plan import (
    AddVariantOp, ArchiveFeatureOp, CreateFeatureOp, CreateMvFeatureOp, CreateSegmentOp, DeleteSegmentOp,
    DeleteSegmentOverrideOp, Plan, SegmentRule, SetFlagOp, SetMvWeightsOp, SetSegmentOverrideOp,
    UpdateSegmentRulesOp,
)

_UUID_NS = uuid.UUID("6b1e0c1a-52b0-4f7d-8d0e-7f1a9d3a4c11")  # stable fake uuids for new objects


class OpError(ValueError):
    """An op cannot be applied to the current document."""


@dataclass
class ApplyResult:
    document: dict[str, Any]
    notes: list[str] = field(default_factory=list)          # informational ("archived features are still served")
    assumptions: list[str] = field(default_factory=list)    # guesses about server-assigned ids
    errors: list[str] = field(default_factory=list)         # first failing op stops application
    segment_refs: dict[str, str] = field(default_factory=dict)   # ref (without "@") -> segment name
    feature_refs: dict[str, str] = field(default_factory=dict)   # ref (without "@") -> feature name
    created_segment_ids: dict[str, int] = field(default_factory=dict)  # segment name -> id used in the prediction
    assumed_split_segments: list[str] = field(default_factory=list)    # created with PERCENTAGE_SPLIT and a guessed id


def _fake_uuid(*parts: object) -> str:
    return str(uuid.uuid5(_UUID_NS, "/".join(str(p) for p in parts)))


def _rule_to_doc(rule: SegmentRule) -> dict[str, Any]:
    """Plan SegmentRule ({property, operator, value}) -> environment-document rule ({property_, ...})."""
    return {
        "type": rule.type,
        "rules": [_rule_to_doc(r) for r in rule.rules],
        "conditions": [{"operator": c.operator, "value": c.value, "property_": c.property or ""}
                       for c in rule.conditions],
    }




def _uses_split(rules: Iterable[SegmentRule]) -> bool:
    return any(c.operator == "PERCENTAGE_SPLIT" for r in rules for c in r.conditions) \
        or any(_uses_split(r.rules) for r in rules)


class _Doc:
    def __init__(self, document: dict[str, Any], hints: Mapping[str, Any]):
        self.d = document
        self.hints = hints
        self.res = ApplyResult(document=document)

    # --- lookups -----------------------------------------------------------------------------
    @property
    def segments(self) -> list[dict[str, Any]]:
        return self.d["project"]["segments"]

    @property
    def env_states(self) -> list[dict[str, Any]]:
        return self.d["feature_states"]

    def feature_name(self, ref: str) -> str:
        if ref.startswith("@"):
            name = self.res.feature_refs.get(ref[1:])
            if name is None:
                raise OpError(f"unknown feature reference {ref!r} (no earlier create_feature with that ref)")
            return name
        return ref

    def feature_state(self, ref: str) -> dict[str, Any]:
        name = self.feature_name(ref)
        for fs in self.env_states:
            if fs["feature"]["name"] == name:
                return fs
        raise OpError(f"feature {name!r} does not exist in the environment")

    def has_feature(self, name: str) -> bool:
        return any(fs["feature"]["name"] == name for fs in self.env_states)

    def segment(self, ref: str) -> dict[str, Any]:
        """Segment by name, numeric id, or @ref (resolved against earlier create_segment ops)."""
        if ref.startswith("@"):
            name = self.res.segment_refs.get(ref[1:])
            if name is None:
                raise OpError(f"unknown segment reference {ref!r} (no earlier create_segment with that ref)")
            ref = name
        for s in self.segments:
            if s["name"] == ref:
                return s
        if ref.isdigit():
            for s in self.segments:
                if s["id"] == int(ref):
                    return s
        raise OpError(f"segment {ref!r} does not exist")

    def _max_id(self, items: Iterable[Mapping[str, Any]], key: str = "id") -> int:
        return max([int(i[key]) for i in items if i.get(key) is not None] + [0])

    def next_segment_id(self) -> int:
        return self._max_id(self.segments) + 1

    def next_feature_id(self) -> int:
        ids = [fs["feature"]["id"] for fs in self.env_states]
        for s in self.segments:
            ids += [fs["feature"]["id"] for fs in s.get("feature_states") or ()]
        return max(ids + [0]) + 1

    def next_django_id(self) -> int:
        ids = [fs.get("django_id") or 0 for fs in self.env_states]
        for s in self.segments:
            ids += [fs.get("django_id") or 0 for fs in s.get("feature_states") or ()]
        return max(ids + [0]) + 1

    def next_mv_id(self) -> int:
        ids = []
        for fs in self.env_states:
            ids += [m.get("id") or 0 for m in fs.get("multivariate_feature_state_values") or ()]
            ids += [m["multivariate_feature_option"].get("id") or 0
                    for m in fs.get("multivariate_feature_state_values") or ()]
        return max(ids + [0]) + 1

    # --- ops ---------------------------------------------------------------------------------
    def _new_feature_state(self, name: str, ftype: str, enabled: bool, value: Any) -> dict[str, Any]:
        guess = self.hints.get("feature_state_ids", {}).get(name)
        django_id = guess if guess is not None else self.next_django_id()
        if guess is None and ftype == "MULTIVARIATE":
            self.res.assumptions.append(
                f"feature {name!r}: server-assigned feature-state id assumed to be {django_id} "
                f"(it feeds the multivariate hash)")
        return {
            "feature": {"id": self.next_feature_id(), "name": name, "type": ftype},
            "enabled": bool(enabled), "django_id": django_id, "feature_segment": None,
            "featurestate_uuid": _fake_uuid("fs", name),
            "feature_state_value": value, "multivariate_feature_state_values": [],
        }

    def op_create_feature(self, op: CreateFeatureOp) -> None:
        if self.has_feature(op.name):
            raise OpError(f"create_feature: {op.name!r} already exists")
        self.env_states.append(self._new_feature_state(op.name, "STANDARD", op.default_enabled, op.initial_value))
        if op.ref:
            self.res.feature_refs[op.ref] = op.name

    def _variant_entries(self, fs: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(m["multivariate_feature_option"]["value"]): m for m in fs["multivariate_feature_state_values"]}

    def _control(self, fs: dict[str, Any], op_name: str) -> str:
        if fs.get("feature_state_value") is None:
            raise OpError(f"{op_name}: {fs['feature']['name']!r} has no control value")
        return str(fs["feature_state_value"])

    def _check_variant_total(self, fs: dict[str, Any], op_name: str) -> None:
        total = sum(float(m["percentage_allocation"]) for m in fs["multivariate_feature_state_values"])
        if total > 100 + 1e-9:
            raise OpError(f"{op_name}: variant weights add up to {total:g} (> 100)")

    def op_create_mv_feature(self, op: CreateMvFeatureOp) -> None:
        if self.has_feature(op.name):
            raise OpError(f"create_mv_feature: {op.name!r} already exists")
        fs = self._new_feature_state(op.name, "MULTIVARIATE", op.default_enabled, op.initial_value)
        self.env_states.append(fs)
        variants = {k: w for k, w in op.weights.items() if k != op.initial_value}
        next_id = self.next_mv_id()
        for i, (value, weight) in enumerate(variants.items()):
            fs["multivariate_feature_state_values"].append({
                "multivariate_feature_option": {"value": value, "id": next_id + i, "key": None},
                "percentage_allocation": float(weight), "id": next_id + i,
                "mv_fs_value_uuid": _fake_uuid("mv", op.name, value)})
        if variants:
            self.res.assumptions.append(
                f"feature {op.name!r}: variant ids assumed {next_id}..{next_id + len(variants) - 1} "
                f"(they order the multivariate buckets)")
        if op.ref:
            self.res.feature_refs[op.ref] = op.name

    def op_add_variant(self, op: AddVariantOp) -> None:
        fs = self.feature_state(op.feature)
        if fs["feature"]["type"] != "MULTIVARIATE":
            raise OpError(f"add_variant: {op.feature!r} is not a multivariate feature")
        mvs = fs["multivariate_feature_state_values"]
        if op.value in self._variant_entries(fs) or op.value == str(fs.get("feature_state_value")):
            raise OpError(f"add_variant: variant {op.value!r} already exists on {op.feature!r}")
        nid = self.next_mv_id()
        mvs.append({"multivariate_feature_option": {"value": op.value, "id": nid, "key": op.key},
                    "percentage_allocation": float(op.weight), "id": nid,
                    "mv_fs_value_uuid": _fake_uuid("mv", op.feature, op.value)})
        self.res.assumptions.append(f"feature {op.feature!r}: new variant {op.value!r} id assumed {nid}")
        self._check_variant_total(fs, "add_variant")

    def op_set_flag(self, op: SetFlagOp) -> None:
        fs = self.feature_state(op.feature)
        fs["enabled"] = op.enabled
        fs["feature_state_value"] = op.value

    def op_set_mv_weights(self, op: SetMvWeightsOp) -> None:
        fs = self.feature_state(op.feature)
        if fs["feature"]["type"] != "MULTIVARIATE":
            raise OpError(f"set_mv_weights: {op.feature!r} is not a multivariate feature")
        control = self._control(fs, "set_mv_weights")
        variants = self._variant_entries(fs)
        expected = set(variants) | {control}
        if set(op.weights) != expected:
            raise OpError(f"set_mv_weights: weights must name every variant including the control "
                          f"(expected {sorted(expected)}, got {sorted(op.weights)})")
        for value, entry in variants.items():
            entry["percentage_allocation"] = float(op.weights[value])
        # the control gets the remainder; the schema guarantees the weights sum to 100, so it equals weights[control]
        self._check_variant_total(fs, "set_mv_weights")

    def op_create_segment(self, op: CreateSegmentOp) -> None:
        if any(s["name"] == op.name for s in self.segments):
            raise OpError(f"create_segment: segment {op.name!r} already exists")
        hinted = self.hints.get("segment_ids", {}).get(op.name)
        sid = int(hinted) if hinted is not None else self.next_segment_id()
        if hinted is None and _uses_split(op.rules):
            self.res.assumed_split_segments.append(op.name)
            self.res.assumptions.append(
                f"segment {op.name!r}: server-assigned id assumed to be {sid} "
                f"(PERCENTAGE_SPLIT conditions hash on the segment id)")
        self.segments.append({"id": sid, "name": op.name,
                              "rules": [_rule_to_doc(r) for r in op.rules], "feature_states": []})
        self.res.created_segment_ids[op.name] = sid
        if op.ref:
            self.res.segment_refs[op.ref] = op.name

    def op_update_segment_rules(self, op: UpdateSegmentRulesOp) -> None:
        self.segment(op.segment)["rules"] = [_rule_to_doc(r) for r in op.rules]

    def op_set_segment_override(self, op: SetSegmentOverrideOp) -> None:
        fs = self.feature_state(op.feature)
        feature = fs["feature"]
        seg = self.segment(op.segment)
        overrides = seg.setdefault("feature_states", [])
        existing = next((o for o in overrides if o["feature"]["name"] == feature["name"]), None)
        mvs: list[dict[str, Any]] = []
        if op.weights:
            if feature["type"] != "MULTIVARIATE":
                raise OpError(f"set_segment_override: weights given but {feature['name']!r} is not multivariate")
            control = None if op.value is None else str(op.value)
            nid = self.next_mv_id()
            for i, (value, weight) in enumerate((k, w) for k, w in op.weights.items() if k != control):
                mvs.append({"multivariate_feature_option": {"value": value, "id": nid + i, "key": None},
                            "percentage_allocation": float(weight), "id": nid + i,
                            "mv_fs_value_uuid": _fake_uuid("ovmv", feature["name"], seg["name"], value)})
        state = {"feature": dict(feature), "enabled": op.enabled,
                 "django_id": None, "feature_segment": {"priority": op.priority},
                 "featurestate_uuid": _fake_uuid("ov", feature["name"], seg["name"]),
                 "feature_state_value": op.value, "multivariate_feature_state_values": mvs}
        if existing is not None:
            overrides[overrides.index(existing)] = state
        else:
            overrides.append(state)

    def op_delete_segment_override(self, op: DeleteSegmentOverrideOp) -> None:
        seg = self.segment(op.segment)
        fname = self.feature_name(op.feature)
        before = len(seg.get("feature_states") or ())
        seg["feature_states"] = [o for o in seg.get("feature_states") or () if o["feature"]["name"] != fname]
        if len(seg["feature_states"]) == before:
            raise OpError(f"delete_segment_override: segment {seg['name']!r} has no override for {fname!r}")

    def op_delete_segment(self, op: DeleteSegmentOp) -> None:
        self.segments.remove(self.segment(op.segment))

    def op_archive_feature(self, op: ArchiveFeatureOp) -> None:
        name = self.feature_state(op.feature)["feature"]["name"]
        self.res.notes.append(
            f"archive_feature {name!r}: Flagsmith keeps serving archived features to SDKs, "
            f"so no served-value change is predicted")


def apply_ops(document: Mapping[str, Any], plan: Plan, hints: Mapping[str, Any] | None = None) -> ApplyResult:
    """Return a deep-copied document with ``plan.ops`` applied. The input is never mutated.

    ``hints`` (all optional): ``{"segment_ids": {name: id}, "feature_state_ids": {feature: django_id}}``
    - use them when the real ids are known (e.g. re-prediction against the post-apply document).
    """
    doc = _Doc(copy.deepcopy(dict(document)), hints or {})
    for i, op in enumerate(plan.ops):
        handler = getattr(doc, f"op_{op.op}", None)
        try:
            if handler is None:
                raise OpError(f"unsupported op {op.op!r}")
            handler(op)
        except OpError as exc:
            doc.res.errors.append(f"ops[{i}] {op.op}: {exc}")
            break
    doc.res.document = doc.d
    return doc.res
