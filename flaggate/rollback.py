"""Rollback generation (inverse ops, in the v1 Op vocabulary) and the derived rollback Plan."""

from __future__ import annotations

from dataclasses import dataclass, field

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
    Plan,
    Rollback,
    SegmentRule,
    SetFlagOp,
    SetMvWeightsOp,
    SetSegmentOverrideOp,
    UpdateSegmentRulesOp,
    plan_hash,
)
from flaggate.state import Refs, Snapshot, apply_op


@dataclass
class RollbackResult:
    ops: list[Op]
    irreversible: list[str] = field(default_factory=list)  # human-readable notes


def _rules(rules: list[dict]) -> list[SegmentRule]:
    return [SegmentRule.model_validate(r) for r in rules]


def inverse_ops(state: Snapshot, op: Op, refs: Refs) -> tuple[list[Op], list[str]]:
    """Ops that undo ``op`` when run right after it, computed from the state *before* ``op``."""
    notes: list[str] = []
    if isinstance(op, (CreateFeatureOp, CreateMvFeatureOp)):
        # the feature did not exist before this plan, so its rollback deletes it (delete_feature is rollback-only)
        return [DeleteFeatureOp(op="delete_feature", feature=op.name)], notes
    if isinstance(op, AddVariantOp):
        name = refs.feature(state, op.feature)
        f = state.features[name]
        w = dict(f.env.weights)
        w[op.value] = 0.0
        notes.append(f"{name}: rollback sets new variant {op.value!r} to 0% (v1 has no delete_variant)")
        return [SetMvWeightsOp(op="set_mv_weights", feature=name, weights=w)], notes
    if isinstance(op, SetFlagOp):
        name = refs.feature(state, op.feature)
        e = state.features[name].env
        return [SetFlagOp(op="set_flag", feature=name, enabled=e.enabled, value=e.value)], notes
    if isinstance(op, SetMvWeightsOp):
        name = refs.feature(state, op.feature)
        return [SetMvWeightsOp(op="set_mv_weights", feature=name, weights=dict(state.features[name].env.weights))], notes
    if isinstance(op, CreateSegmentOp):
        return [DeleteSegmentOp(op="delete_segment", segment=op.name)], notes
    if isinstance(op, UpdateSegmentRulesOp):
        name = refs.segment(state, op.segment)
        return [UpdateSegmentRulesOp(op="update_segment_rules", segment=name, rules=_rules(state.segments[name].rules))], notes
    if isinstance(op, SetSegmentOverrideOp):
        fname, sname = refs.feature(state, op.feature), refs.segment(state, op.segment)
        prev = state.features[fname].overrides.get(sname)
        if prev is None:
            return [DeleteSegmentOverrideOp(op="delete_segment_override", feature=fname, segment=sname)], notes
        return [SetSegmentOverrideOp(op="set_segment_override", feature=fname, segment=sname, enabled=prev.enabled,
                                     value=prev.value, priority=prev.priority, weights=prev.weights)], notes
    if isinstance(op, DeleteSegmentOverrideOp):
        fname, sname = refs.feature(state, op.feature), refs.segment(state, op.segment)
        prev = state.features[fname].overrides[sname]
        return [SetSegmentOverrideOp(op="set_segment_override", feature=fname, segment=sname, enabled=prev.enabled,
                                     value=prev.value, priority=prev.priority, weights=prev.weights)], notes
    if isinstance(op, DeleteSegmentOp):
        sname = refs.segment(state, op.segment)
        out: list[Op] = [CreateSegmentOp(op="create_segment", name=sname, rules=_rules(state.segments[sname].rules))]
        for fname, f in sorted(state.features.items()):
            ov = f.overrides.get(sname)
            if ov is not None:
                out.append(SetSegmentOverrideOp(op="set_segment_override", feature=fname, segment=sname,
                                                enabled=ov.enabled, value=ov.value, priority=ov.priority, weights=ov.weights))
        notes.append(f"segment {sname}: rollback re-creates it with a new id")
        return out, notes
    if isinstance(op, ArchiveFeatureOp):
        name = refs.feature(state, op.feature)
        notes.append(f"{name}: archive_feature cannot be undone in v1 (no unarchive op)")
        return [], notes
    if isinstance(op, DeleteFeatureOp):
        raise ValueError("delete_feature is rollback-only; it cannot appear in a forward plan")
    raise ValueError(f"unsupported op {op!r}")  # pragma: no cover


def created_features(plan: Plan) -> set[str]:
    return {op.name for op in plan.ops if isinstance(op, (CreateFeatureOp, CreateMvFeatureOp))}


def check_delete_feature(plan: Plan) -> None:
    """delete_feature may only undo a feature created by ``plan`` itself. Raises ValueError otherwise."""
    if any(isinstance(op, DeleteFeatureOp) for op in plan.ops):
        raise ValueError("delete_feature is rollback-only; it cannot appear in a submitted plan")
    created = created_features(plan)
    for op in plan.rollback.ops:
        if isinstance(op, DeleteFeatureOp) and op.feature not in created:
            raise ValueError(f"delete_feature {op.feature!r}: only features created by this plan can be deleted")


def generate_rollback(before: Snapshot, ops: list[Op]) -> RollbackResult:
    state = before.model_copy(deep=True)
    refs = Refs()
    chunks: list[list[Op]] = []
    notes: list[str] = []
    for op in ops:
        inv, n = inverse_ops(state, op, refs)
        chunks.append(inv)
        notes.extend(n)
        apply_op(state, op, refs)
    return RollbackResult(ops=[o for chunk in reversed(chunks) for o in chunk], irreversible=notes)


def rollback_plan(plan: Plan) -> Plan:
    """Deterministic plan that runs ``plan.rollback.ops``. Approver and gatekeeper derive the same hash.

    Its ``base_state_hash`` is ``post:<plan_hash>``: the gatekeeper accepts it only while the live state
    still equals the state recorded right after ``plan`` was applied.
    """
    if not plan.rollback.ops:
        raise ValueError(f"plan {plan.plan_id} has no rollback ops")
    return Plan(
        plan_id=f"{plan.plan_id}.rollback",
        created_at=plan.created_at,
        author="flaggate:rollback",
        prompt=f"rollback of {plan.plan_id}",
        target=plan.target,
        base_state_hash=f"post:{plan_hash(plan)}",
        ops=list(plan.rollback.ops),
        rollback=Rollback(ops=[]),
        guardrails=plan.guardrails,
        expires_at=None,
    )


