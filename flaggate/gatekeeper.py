"""The gatekeeper: the only component that holds the Flagsmith Admin API key and writes to Flagsmith.

It applies a plan only when an approval token signed by the pinned approver key is bound to the
exact plan hash, unexpired, unused, and the live state still matches ``base_state_hash``. Every
decision (submit / refuse / apply / failure / rollback) is written to the hash-chained audit log.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flaggate.approval import ApprovalError, fingerprint, verify_token
from flaggate.audit import AuditLog
from flaggate.client import FlagsmithClient
from flaggate.diff import diff_states, render_text
from flaggate.executor import ApplyResult, Executor
from flaggate.hooks import ImpactEngine
from flaggate.plan import Plan, Target, plan_hash
from flaggate.planner import plan_from_prompt
from flaggate.rollback import generate_rollback, rollback_plan
from flaggate.state import PlanConflict, Snapshot, env_fingerprint, simulate, state_hash, take_snapshot

ARCHIVE_NOTE = ("archive_feature only hides the flag in the dashboard: Flagsmith keeps serving it to SDKs "
                "(no change in evaluation). Disable it with set_flag if users should stop getting it.")


class Refused(Exception):
    """Apply/rollback refused. ``reason`` is a short machine code, ``detail`` is human text."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"REFUSED ({reason}): {detail}")
        self.reason, self.detail = reason, detail


@dataclass(frozen=True)
class GatekeeperConfig:
    api_url: str
    organisation_id: int
    project_id: int
    environment: str
    env_key: str  # client-side env key (used in API paths); kept out of logs
    server_key: str = ""  # server-side env key: environment document for the impact engine

    @property
    def env_fp(self) -> str:
        return env_fingerprint(self.api_url, self.env_key)

    def target(self) -> Target:
        return Target(api_url=self.api_url.rstrip("/"), organisation_id=self.organisation_id,
                      project_id=self.project_id, environment=self.environment, env_fp=self.env_fp)


def harden_process() -> bool:
    """prctl(PR_SET_DUMPABLE, 0): same-UID processes can no longer ptrace us or read /proc/<pid>/mem|environ."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE = 4
    except Exception:  # noqa: BLE001 - non-Linux
        return False


class Gatekeeper:
    def __init__(
        self,
        *,
        state_dir: Path,
        config: GatekeeperConfig,
        approver_pub: bytes,
        client: FlagsmithClient | None = None,
        impact: ImpactEngine,
        snapshot_fn: Callable[[], Snapshot] | None = None,
        executor_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        max_token_ttl_s: int = 24 * 3600 + 60,
    ):
        self.dir = Path(state_dir)
        (self.dir / "plans").mkdir(parents=True, exist_ok=True)
        self.cfg = config
        self.pub = approver_pub
        self.client = client
        self._snapshot_fn = snapshot_fn
        self._executor_factory = executor_factory
        self.impact = impact
        self.clock = clock
        self.max_token_ttl_s = max_token_ttl_s
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.approver_fp = fingerprint(approver_pub)

    # ------------------------------------------------------------------ plumbing
    def snapshot(self) -> Snapshot:
        if self._snapshot_fn:
            return self._snapshot_fn()
        assert self.client is not None
        return take_snapshot(self.client, self.cfg.target(), self.cfg.env_key)

    def _executor(self) -> Any:
        if self._executor_factory:
            return self._executor_factory()
        assert self.client is not None
        return Executor(self.client, self.cfg.env_key)

    def _preview(self, plan: Plan) -> Any:
        try:
            return self.impact.preview(plan)
        except Exception as e:  # noqa: BLE001 - fail closed
            raise Refused("impact_error", f"impact preview failed: {e}") from None

    def _plan_path(self, plan_id: str) -> Path:
        if "/" in plan_id or plan_id.startswith("."):
            raise ValueError("bad plan id")
        return self.dir / "plans" / f"{plan_id}.json"

    def _meta_path(self, plan_id: str) -> Path:
        return self.dir / "plans" / f"{plan_id}.meta.json"

    def _write_json(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def meta(self, plan_id: str) -> dict[str, Any]:
        p = self._meta_path(plan_id)
        return json.loads(p.read_text()) if p.exists() else {}

    def _set_meta(self, plan_id: str, **kv: Any) -> None:
        m = self.meta(plan_id)
        m.update(kv)
        self._write_json(self._meta_path(plan_id), m)

    # ------------------------------------------------------------------ nonces
    def _consume_nonce(self, nonce: str) -> bool:
        """Atomically record ``nonce``; False if it was already used."""
        import fcntl
        path = self.dir / "nonces.txt"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            if nonce in {line.strip() for line in f}:
                return False
            f.write(nonce + "\n")
            f.flush()
            os.fsync(f.fileno())
            return True

    # ------------------------------------------------------------------ submit / read
    def submit(self, plan: Plan, actor: str = "agent:mcp") -> dict[str, Any]:
        """Validate, attach generated rollback + impact, store, audit. Returns the review bundle."""
        if plan.target != self.cfg.target():
            self.audit.append("refused", plan_id=plan.plan_id, action="submit", reason="target",
                              detail="plan target does not match this gatekeeper's instance/project/environment")
            raise Refused("target", "plan target does not match this gatekeeper")
        before = self.snapshot()
        live = state_hash(before)
        if plan.base_state_hash != live:
            self.audit.append("refused", plan_id=plan.plan_id, action="submit", reason="stale",
                              detail="base_state_hash != live state", live_state_hash=live)
            raise Refused("stale", "plan was built on an older state; re-plan against a fresh snapshot")
        try:
            after = simulate(before, plan.ops)
        except PlanConflict as e:
            self.audit.append("refused", plan_id=plan.plan_id, action="submit", reason="invalid", detail=str(e))
            raise Refused("invalid", str(e)) from None
        rb = generate_rollback(before, list(plan.ops))
        stored = plan.model_copy(update={"rollback": plan.rollback.model_copy(update={"ops": rb.ops}),
                                         "impact": None, "status": "submitted", "plan_hash": None})
        h = plan_hash(stored)
        path = self._plan_path(plan.plan_id)
        if path.exists():
            existing = Plan.model_validate_json(path.read_text())
            if plan_hash(existing) != h:
                raise Refused("duplicate", f"plan id {plan.plan_id} already exists with different content")
        res = self._preview(stored)
        stored = stored.model_copy(update={"impact": res.report, "plan_hash": h})
        self._write_json(path, json.loads(stored.model_dump_json(by_alias=True)))
        changes = diff_states(before, after)
        notes = list(rb.irreversible)
        if any(op.op == "archive_feature" for op in plan.ops):
            notes.insert(0, ARCHIVE_NOTE)
        self._set_meta(plan.plan_id, status="submitted", plan_hash=h, rollback_notes=notes)
        self.audit.append("plan_submitted", plan_id=plan.plan_id, plan_hash=h, actor=actor,
                          author=plan.author, ops=len(plan.ops), rollback_ops=len(rb.ops),
                          base_state_hash=plan.base_state_hash, approvable=bool(res.approvable),
                          max_exposure_pct=res.report.get("max_exposure_pct"))
        return self._bundle(stored, before, changes, notes)

    def _bundle(self, plan: Plan, before: Snapshot, changes: list, notes: list[str]) -> dict[str, Any]:
        rb = rollback_plan(plan) if plan.rollback.ops else None
        return {
            "plan_id": plan.plan_id,
            "plan_hash": plan_hash(plan),
            "rollback_plan_hash": plan_hash(rb) if rb else None,
            "plan": json.loads(plan.model_dump_json(by_alias=True)),
            "diff": [c.model_dump() for c in changes],
            "diff_text": render_text(changes),
            "impact": plan.impact,
            "approvable": bool((plan.impact or {}).get("approvable", False)),
            "rollback_notes": notes,
            "manual_steps_replaced": len(plan.ops) + len(changes),
            "status": self.meta(plan.plan_id).get("status"),
        }

    def get_plan(self, plan_id: str) -> Plan:
        path = self._plan_path(plan_id)
        if not path.exists():
            raise Refused("unknown_plan", f"no plan {plan_id}")
        plan = Plan.model_validate_json(path.read_text())
        meta_hash = self.meta(plan_id).get("plan_hash")
        if plan_hash(plan) != meta_hash or plan.plan_hash != meta_hash:
            raise Refused("tampered", f"stored plan {plan_id} no longer matches the hash recorded at submit")
        return plan

    def review(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        before = self.snapshot()
        try:
            changes = diff_states(before, simulate(before, plan.ops))
        except PlanConflict as e:
            changes = []
            notes = [f"plan no longer applies to live state: {e}"]
        else:
            notes = self.meta(plan_id).get("rollback_notes", [])
        b = self._bundle(plan, before, changes, notes)
        b["snapshot"] = json.loads(before.model_dump_json())
        b["live_state_hash"] = state_hash(before)
        return b

    # ------------------------------------------------------------------ token checks
    def _check_token(self, plan: Plan, token: str | None, action: str, expected_hash: str) -> dict[str, Any]:
        try:
            payload = verify_token(self.pub, token)
        except ApprovalError as e:
            raise Refused("no_valid_approval" if not token else "bad_signature", str(e)) from None
        now = self.clock()
        if payload.get("action") != action:
            raise Refused("wrong_action", f"token is for {payload.get('action')!r}, not {action!r}")
        if payload.get("plan_id") != plan.plan_id:
            raise Refused("hash_mismatch", "token was issued for a different plan id")
        if payload.get("plan_hash") != expected_hash:
            raise Refused("hash_mismatch", "token plan_hash does not match the stored plan (plan changed after approval?)")
        if payload.get("env_fp") != self.cfg.env_fp:
            raise Refused("wrong_environment", "token is bound to another environment")
        exp, iat = int(payload.get("exp", 0)), int(payload.get("iat", 0))
        if exp <= now:
            raise Refused("expired", "approval token has expired; approve again")
        if exp - iat > self.max_token_ttl_s:
            raise Refused("ttl_too_long", "token lifetime exceeds gatekeeper maximum")
        if not self._consume_nonce(str(payload.get("nonce"))):
            raise Refused("token_reused", "approval token was already used")
        return payload

    def _refuse(self, plan_id: str, action: str, e: Refused, **extra: Any) -> None:
        self.audit.append("refused", plan_id=plan_id, action=action, reason=e.reason, detail=e.detail, **extra)

    # ------------------------------------------------------------------ apply
    @staticmethod
    def _phases(before: Snapshot, ops: list) -> tuple[list[int], list[int]]:
        """Two-phase split: create_segment ops first (inert until an override exists), then the rest in order.

        PERCENTAGE_SPLIT hashes on the server-assigned segment id, so the impact preview can only be exact once
        the segments exist. Falls back to a single phase if moving the segment creations first would change
        the plan's meaning (simulation of the reordered ops fails or differs)."""
        p1 = [i for i, op in enumerate(ops) if op.op == "create_segment"]
        if not p1:
            return [], list(range(len(ops)))
        p2 = [i for i in range(len(ops)) if i not in p1]
        try:
            same = state_hash(simulate(before, [ops[i] for i in p1 + p2])) == state_hash(simulate(before, ops))
        except PlanConflict:
            same = False
        return (p1, p2) if same else ([], list(range(len(ops))))

    def apply(self, plan_id: str, token: str | None) -> dict[str, Any]:
        try:
            plan = self.get_plan(plan_id)
            h = plan_hash(plan)
            payload = self._check_token(plan, token, "apply", h)
            if self.meta(plan_id).get("status") in ("applied", "rolled_back", "halted", "apply_failed"):
                raise Refused("already_used", f"plan {plan_id} already went through apply; re-plan")
            if plan.expires_at and plan.expires_at.timestamp() <= self.clock():
                raise Refused("plan_expired", "plan expired; re-plan")
            before = self.snapshot()
            live = state_hash(before)
            if live != plan.base_state_hash or payload.get("base_state_hash") != plan.base_state_hash:
                raise Refused("drift", "live Flagsmith state changed since the plan was made; re-plan")
            preview = self._preview(plan)
            if not preview.approvable:
                raise Refused("guardrails", "impact preview is not approvable: "
                              + "; ".join(map(str, preview.report.get("blockers") or [])) or "guardrails failed")
        except Refused as e:
            self._refuse(plan_id, "apply", e)
            raise
        actor = f"human:{payload.get('approver')} ({self.approver_fp})"
        self.audit.append("approved", plan_id=plan_id, plan_hash=h, action="apply", actor=actor,
                          token_nonce=payload["nonce"], max_exposure_pct=preview.report.get("max_exposure_pct"))
        ops = list(plan.ops)
        p1, p2 = self._phases(before, ops)
        sess = self._executor().session(before)
        final = preview
        if p1:
            r1 = sess.run(p1, ops)
            if not r1.ok:
                return self._failed(plan_id, h, actor, r1, phase=1)
            try:
                final = self.impact.repredict(preview)
                ok, why = bool(final.approvable), "; ".join(map(str, final.report.get("blockers") or []))
            except Exception as e:  # noqa: BLE001 - fail closed
                ok, why = False, f"re-prediction failed: {e}"
            if not ok:
                errors = sess.compensate()
                self._set_meta(plan_id, status="halted")
                rec = self.audit.append(
                    "halted", plan_id=plan_id, plan_hash=h, actor=actor, via="gatekeeper", phase=1,
                    reason="guardrails_after_live_ids",
                    detail=why or "re-prediction with server-assigned segment ids is not approvable",
                    approved_max_exposure_pct=preview.report.get("max_exposure_pct"),
                    live_ids_max_exposure_pct=getattr(final, "report", {}).get("max_exposure_pct"),
                    created_segments_removed=not errors, compensation_errors=errors, flagsmith_calls=sess.calls())
                return {"ok": False, "status": "halted", "reason": "guardrails_after_live_ids", "detail": why,
                        "compensated": not errors, "audit_seq": rec["seq"]}
        r2 = sess.run(p2, ops)
        if not r2.ok:
            return self._failed(plan_id, h, actor, r2, phase=2 if p1 else 1)
        calls = sess.calls()
        after = self.snapshot()
        result_hash = state_hash(after)
        try:
            verify = self.impact.verify(final)
        except Exception as e:  # noqa: BLE001
            verify = {"ok": False, "error": str(e)}
        self._set_meta(plan_id, status="applied", result_state_hash=result_hash, applied_at=self.clock())
        rec = self.audit.append("applied", plan_id=plan_id, plan_hash=h, action="apply", actor=actor,
                                via="gatekeeper", token_nonce=payload["nonce"], two_phase=bool(p1),
                                flagsmith_calls=calls, result_state_hash=result_hash,
                                predicted_max_exposure_pct=final.report.get("max_exposure_pct"),
                                prediction_match=verify.get("prediction_match"), verify_ok=verify.get("ok"))
        return {"ok": True, "status": "applied", "two_phase": bool(p1), "flagsmith_calls": len(calls),
                "result_state_hash": result_hash, "verify": verify, "audit_seq": rec["seq"]}

    def _failed(self, plan_id: str, h: str, actor: str, res: ApplyResult, phase: int) -> dict[str, Any]:
        self._set_meta(plan_id, status="apply_failed")
        rec = self.audit.append("apply_failed", plan_id=plan_id, plan_hash=h, actor=actor, via="gatekeeper",
                                phase=phase, error=res.error, failed_op=res.failed_op, compensated=res.compensated,
                                compensation_errors=res.compensation_errors, flagsmith_calls=res.calls)
        return {"ok": False, "status": "apply_failed", "error": res.error, "compensated": res.compensated,
                "compensation_errors": res.compensation_errors, "audit_seq": rec["seq"]}

    # ------------------------------------------------------------------ rollback
    def rollback(self, plan_id: str, token: str | None) -> dict[str, Any]:
        try:
            plan = self.get_plan(plan_id)
            if not plan.rollback.ops:
                raise Refused("no_rollback", "plan has no rollback ops")
            rb = rollback_plan(plan)
            payload = self._check_token(rb, token, "rollback", plan_hash(rb))
            meta = self.meta(plan_id)
            if meta.get("status") != "applied":
                raise Refused("not_applied", f"plan status is {meta.get('status')!r}, nothing to roll back")
            before = self.snapshot()
            if state_hash(before) != meta.get("result_state_hash"):
                raise Refused("drift", "state changed since this plan was applied; rollback needs a fresh plan")
        except Refused as e:
            self._refuse(plan_id, "rollback", e)
            raise
        actor = f"human:{payload.get('approver')} ({self.approver_fp}) [pre-approved rollback]"
        sess = self._executor().session(before)
        res = sess.run(list(range(len(rb.ops))), list(rb.ops))
        if not res.ok:
            rec = self.audit.append("apply_failed", plan_id=rb.plan_id, plan_hash=plan_hash(rb), action="rollback",
                                    actor=actor, error=res.error, compensated=res.compensated,
                                    compensation_errors=res.compensation_errors, flagsmith_calls=res.calls)
            return {"ok": False, "status": "rollback_failed", "error": res.error, "audit_seq": rec["seq"]}
        after = self.snapshot()
        self._set_meta(plan_id, status="rolled_back", rolled_back_state_hash=state_hash(after))
        rec = self.audit.append("rolled_back", plan_id=plan_id, plan_hash=plan_hash(rb), action="rollback", actor=actor,
                                via="gatekeeper", token_nonce=payload["nonce"], flagsmith_calls=sess.calls(),
                                result_state_hash=state_hash(after), restored_base=state_hash(after) == plan.base_state_hash)
        return {"ok": True, "status": "rolled_back", "flagsmith_calls": len(sess.calls()),
                "restored_base_state": state_hash(after) == plan.base_state_hash, "audit_seq": rec["seq"]}

    # ------------------------------------------------------------------ planner / audit
    def plan_from_prompt(self, prompt: str, actor: str = "planner:rules") -> dict[str, Any]:
        snap = self.snapshot()
        plan = plan_from_prompt(prompt, snap, author=actor)
        return self.submit(plan, actor=actor)

    def audit_verify(self) -> dict[str, Any]:
        ok, n, msg = self.audit.verify()
        out: dict[str, Any] = {"ok": ok, "records": n, "message": msg}
        recs = self.audit.records()
        last = next((r for r in reversed(recs) if r["event"] in ("applied", "rolled_back")), None)
        if ok and last is not None:
            try:
                live = state_hash(self.snapshot())
                out["live_matches_last_change"] = live == last.get("result_state_hash")
            except Exception as e:  # noqa: BLE001
                out["live_check_error"] = str(e)
        return out
