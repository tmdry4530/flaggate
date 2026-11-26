"""Apply v1 plan ops to a live Flagsmith environment, with automatic compensation.

Endpoints: Admin API v1 for features / mv-options / segments / archive, and the
``/api/__future__/environments/{env}/features/{id}/`` flag endpoint (the one the
official Flagsmith CLI v2 uses) for env defaults, MV weights and segment overrides.

Each successful op pushes an undo action. If a later op fails, the undo stack runs
in reverse ("compensation") so a half-applied plan does not stay live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from flaggate.client import FlagsmithClient
from flaggate.plan import (
    AddVariantOp,
    ArchiveFeatureOp,
    CreateFeatureOp,
    CreateMvFeatureOp,
    CreateSegmentOp,
    DeleteSegmentOp,
    DeleteSegmentOverrideOp,
    Op,
    SetFlagOp,
    SetMvWeightsOp,
    SetSegmentOverrideOp,
    UpdateSegmentRulesOp,
)
from flaggate.state import (
    FeatureView,
    OverrideView,
    Refs,
    Snapshot,
    apply_op,
    normalize_rules,
    rules_to_api,
)


def to_api_value(v: Any) -> dict[str, str] | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return {"type": "boolean", "value": "true" if v else "false"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    return {"type": "string", "value": str(v)}


@dataclass
class ApplyResult:
    ok: bool
    applied_ops: int
    calls: list[dict[str, Any]]
    error: str | None = None
    failed_op: int | None = None
    compensated: bool | None = None
    compensation_errors: list[str] = field(default_factory=list)


class Executor:
    def __init__(self, client: FlagsmithClient, env_key: str):
        self.c = client
        self.env_key = env_key

    # --- endpoint helpers -------------------------------------------------
    def _flag(self, fid: int) -> str:
        return f"/api/__future__/environments/{self.env_key}/features/{fid}/"

    def _feat(self, p: int, fid: int | None = None) -> str:
        return f"/api/v1/projects/{p}/features/" + (f"{fid}/" if fid else "")

    def _seg(self, p: int, sid: int | None = None) -> str:
        return f"/api/v1/projects/{p}/segments/" + (f"{sid}/" if sid else "")

    def _variants(self, f: FeatureView, weights: dict[str, float]) -> list[dict[str, Any]]:
        return [{"id": f.variants[k].id, "weight": w} for k, w in sorted(weights.items()) if k in f.variants]

    def _override_body(self, f: FeatureView, seg_id: int, ov: OverrideView) -> dict[str, Any]:
        body: dict[str, Any] = {"segment": {"id": seg_id}, "enabled": ov.enabled,
                                "value": to_api_value(ov.value), "priority": ov.priority}
        if ov.weights is not None:
            body["variants"] = self._variants(f, ov.weights)
        return body

    # --- one op ----------------------------------------------------------
    def _apply_one(self, state: Snapshot, op: Op, refs: Refs) -> Callable[[], None]:
        """Perform ``op`` against Flagsmith, update ``state`` with real ids, and return its undo action."""
        c, p = self.c, state.target.project_id
        before = state.model_copy(deep=True)
        apply_op(state, op, refs)  # validates + computes the intended state (raises PlanConflict)

        if isinstance(op, (CreateFeatureOp, CreateMvFeatureOp)):
            body: dict[str, Any] = {"name": op.name, "default_enabled": op.default_enabled}
            if op.description:
                body["description"] = op.description
            if isinstance(op, CreateFeatureOp):
                body["type"] = "STANDARD"
                if op.initial_value is not None:
                    body["initial_value"] = op.initial_value
            else:
                body.update(type="MULTIVARIATE", initial_value=op.initial_value, multivariate_options=[
                    {"type": "unicode", "string_value": k, "default_percentage_allocation": w}
                    for k, w in op.weights.items() if k != op.initial_value])
            created = c.post(self._feat(p), body)
            fv = state.features[op.name]
            fv.id = created["id"]
            for o in created.get("multivariate_options") or []:
                if o.get("string_value") in fv.variants:
                    fv.variants[o["string_value"]].id = o["id"]
            fid = created["id"]
            return lambda: c.delete(self._feat(p, fid))

        if isinstance(op, AddVariantOp):
            name = refs.feature(state, op.feature)
            f = state.features[name]
            body = {"type": "unicode", "string_value": op.value, "default_percentage_allocation": op.weight, "feature": f.id}
            if op.key:
                body["key"] = op.key
            created = c.post(f"{self._feat(p, f.id)}mv-options/", body)
            f.variants[op.value].id = created["id"]
            # make the env weight explicit (Flagsmith seeds it from default_percentage_allocation)
            c.patch(self._flag(f.id), {"environment_default": {"variants": self._variants(f, f.env.weights)}})
            oid, fid = created["id"], f.id
            return lambda: c.delete(f"{self._feat(p, fid)}mv-options/{oid}/")

        if isinstance(op, SetFlagOp):
            name = refs.feature(state, op.feature)
            f, prev = state.features[name], before.features[name].env
            c.patch(self._flag(f.id), {"environment_default": {"enabled": op.enabled, "value": to_api_value(op.value)}})
            fid = f.id
            return lambda: c.patch(self._flag(fid), {"environment_default": {"enabled": prev.enabled, "value": to_api_value(prev.value)}})

        if isinstance(op, SetMvWeightsOp):
            name = refs.feature(state, op.feature)
            f, prev_f = state.features[name], before.features[name]
            c.patch(self._flag(f.id), {"environment_default": {"variants": self._variants(f, op.weights)}})
            fid, undo_body = f.id, {"environment_default": {"variants": self._variants(prev_f, prev_f.env.weights)}}
            return lambda: c.patch(self._flag(fid), undo_body)

        if isinstance(op, CreateSegmentOp):
            created = c.post(self._seg(p), {"name": op.name, "project": p, "description": op.description,
                                            "rules": rules_to_api(normalize_rules(op.rules))})
            state.segments[op.name].id = created["id"]
            sid = created["id"]
            return lambda: c.delete(self._seg(p, sid))

        if isinstance(op, UpdateSegmentRulesOp):
            name = refs.segment(state, op.segment)
            seg, prev_rules = state.segments[name], before.segments[name].rules
            c.put(self._seg(p, seg.id), {"name": name, "project": p, "rules": rules_to_api(seg.rules)})
            sid = seg.id
            return lambda: c.put(self._seg(p, sid), {"name": name, "project": p, "rules": rules_to_api(prev_rules)})

        if isinstance(op, SetSegmentOverrideOp):
            fname, sname = refs.feature(state, op.feature), refs.segment(state, op.segment)
            f, sid = state.features[fname], state.segments[sname].id
            ov = f.overrides[sname]
            ov.segment_id = sid
            c.patch(self._flag(f.id), {"segment_overrides": [self._override_body(f, sid, ov)]})
            prev = before.features[fname].overrides.get(sname)
            fid = f.id
            if prev is None:
                return lambda: c.delete(f"{self._flag(fid)}segment-overrides/{sid}/")
            undo = {"segment_overrides": [self._override_body(before.features[fname], sid, prev)]}
            return lambda: c.patch(self._flag(fid), undo)

        if isinstance(op, DeleteSegmentOverrideOp):
            fname, sname = refs.feature(before, op.feature), refs.segment(before, op.segment)
            f, sid = before.features[fname], before.segments[sname].id
            c.delete(f"{self._flag(f.id)}segment-overrides/{sid}/")
            undo = {"segment_overrides": [self._override_body(f, sid, f.overrides[sname])]}
            fid = f.id
            return lambda: c.patch(self._flag(fid), undo)

        if isinstance(op, DeleteSegmentOp):
            sname = refs.segment(before, op.segment)
            seg = before.segments[sname]
            c.delete(self._seg(p, seg.id))
            affected = [(fv, fv.overrides[sname]) for fv in before.features.values() if sname in fv.overrides]

            def undo_delete_segment() -> None:
                created = c.post(self._seg(p), {"name": sname, "project": p, "rules": rules_to_api(seg.rules)})
                for fv, ov in affected:
                    c.patch(self._flag(fv.id), {"segment_overrides": [self._override_body(fv, created["id"], ov)]})

            return undo_delete_segment

        if isinstance(op, ArchiveFeatureOp):
            name = refs.feature(state, op.feature)
            fid = state.features[name].id
            c.patch(self._feat(p, fid), {"is_archived": True})
            return lambda: c.patch(self._feat(p, fid), {"is_archived": False})

        raise ValueError(f"unsupported op {op!r}")  # pragma: no cover

    # --- sessions (phased apply) -----------------------------------------
    def session(self, before: Snapshot) -> "ApplySession":
        return ApplySession(self, before)

    def apply(self, before: Snapshot, ops: list[Op], fail_at: int | None = None) -> ApplyResult:
        """Single-phase apply of ``ops`` in order. ``fail_at`` is a test-only fault injection index."""
        s = self.session(before)
        s.fail_at = fail_at
        res = s.run(list(range(len(ops))), ops)
        return res if not res.ok else s.result()


class ApplySession:
    """Shared state/refs/undo stack across phases, so phase 2 can reference segments created in phase 1
    and a failure in any phase compensates everything applied so far."""

    def __init__(self, ex: Executor, before: Snapshot):
        self.ex = ex
        self.state = before.model_copy(deep=True)
        self.refs = Refs()
        self.undo: list[tuple[int, Callable[[], None]]] = []
        self.start = len(ex.c.calls)
        self.fail_at: int | None = None

    def run(self, indices: list[int], ops: list[Op]) -> ApplyResult:
        """Apply ``ops[i]`` for i in ``indices``. On failure compensate everything this session applied."""
        for i in indices:
            op = ops[i]
            try:
                if self.fail_at is not None and i == self.fail_at:
                    raise RuntimeError(f"injected failure at op #{i}")
                self.undo.append((i, self.ex._apply_one(self.state, op, self.refs)))
            except Exception as e:  # noqa: BLE001 - any failure triggers compensation
                errors = self.compensate()
                return ApplyResult(ok=False, applied_ops=0, calls=self.calls(),
                                   error=f"op #{i} ({op.op}): {e}", failed_op=i,
                                   compensated=not errors, compensation_errors=errors)
        return ApplyResult(ok=True, applied_ops=len(self.undo), calls=self.calls())

    def compensate(self) -> list[str]:
        errors: list[str] = []
        for j, action in reversed(self.undo):
            try:
                action()
            except Exception as ue:  # noqa: BLE001
                errors.append(f"undo of op #{j} failed: {ue}")
        self.undo.clear()
        return errors

    def calls(self) -> list[dict[str, Any]]:
        return self.ex.c.calls[self.start:]

    def result(self) -> ApplyResult:
        return ApplyResult(ok=True, applied_ops=len(self.undo), calls=self.calls())
