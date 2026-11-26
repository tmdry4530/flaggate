"""Offline fakes for gatekeeper tests: an in-memory Flagsmith 'world', impact engine and executor."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flaggate.approval import make_payload, public_bytes, sign_token
from flaggate.executor import ApplyResult
from flaggate.gatekeeper import Gatekeeper, GatekeeperConfig
from flaggate.state import EnvState, FeatureView, OverrideView, SegmentView, Snapshot, VariantView, simulate

CFG = GatekeeperConfig(api_url="http://127.0.0.1:8600", organisation_id=1, project_id=1,
                       environment="Development", env_key="TESTENVKEY0000000000ab", server_key="ser.test")


def demo_snapshot() -> Snapshot:
    pro = [{"type": "ALL", "rules": [], "conditions": [{"property": "plan", "operator": "EQUAL", "value": "pro"}]}]
    return Snapshot(
        target=CFG.target(), environment_id=1,
        segments={"pro_users": SegmentView(id=1, rules=pro)},
        features={
            "new_checkout": FeatureView(id=1, env=EnvState(enabled=False),
                                        overrides={"pro_users": OverrideView(segment_id=1, enabled=True, value="")}),
            "checkout_banner": FeatureView(id=2, initial_value="hi", env=EnvState(enabled=True, value="hi")),
            "checkout_button_color": FeatureView(
                id=3, type="MULTIVARIATE", initial_value="blue",
                variants={"green": VariantView(id=1, default_weight=33), "orange": VariantView(id=2, default_weight=33)},
                env=EnvState(enabled=True, value="blue", weights={"blue": 34.0, "green": 33.0, "orange": 33.0})),
        },
    )


@dataclass
class World:
    snap: Snapshot = field(default_factory=demo_snapshot)

    def snapshot(self) -> Snapshot:
        return self.snap.model_copy(deep=True)


class FakeSession:
    def __init__(self, world: World, before: Snapshot, log: list):
        self.world, self.before, self.done, self.log = world, before, [], log

    def run(self, indices: list[int], ops: list) -> ApplyResult:
        for i in indices:
            self.done.append(ops[i])
        self.world.snap = simulate(self.before, self.done)
        self.log.append([ops[i].op for i in indices])
        return ApplyResult(ok=True, applied_ops=len(self.done), calls=[{"method": "FAKE", "path": "/", "status": 200}])

    def compensate(self) -> list[str]:
        self.world.snap = self.before.model_copy(deep=True)
        self.done.clear()
        return []

    def calls(self) -> list:
        return [{"method": "FAKE", "path": "/", "status": 200}]


class FakeExecutor:
    def __init__(self, world: World):
        self.world, self.phases = world, []

    def session(self, before: Snapshot) -> FakeSession:
        return FakeSession(self.world, before, self.phases)


@dataclass
class FakeResult:
    report: dict[str, Any]

    @property
    def approvable(self) -> bool:
        return bool(self.report.get("approvable"))


class FakeImpact:
    def __init__(self, approvable: bool = True, repredict_approvable: bool = True):
        self.approvable, self.repredict_approvable = approvable, repredict_approvable
        self.calls: list[str] = []

    def preview(self, plan) -> FakeResult:
        self.calls.append("preview")
        return FakeResult({"engine": "fake", "approvable": self.approvable, "max_exposure_pct": 5.0,
                           "blockers": [] if self.approvable else ["exposure 80% > 20%"]})

    def repredict(self, result) -> FakeResult:
        self.calls.append("repredict")
        return FakeResult({"engine": "fake", "approvable": self.repredict_approvable, "max_exposure_pct": 30.0,
                           "blockers": [] if self.repredict_approvable else ["exposure 30% > 20% with real segment id"]})

    def verify(self, result) -> dict:
        self.calls.append("verify")
        return {"ok": True, "prediction_match": "10/10"}


class Env:
    """Gatekeeper wired to fakes + an approver key, with helpers to sign tokens."""

    def __init__(self, tmp: Path, **impact_kw: Any):
        self.world = World()
        self.key = Ed25519PrivateKey.generate()
        self.impact = FakeImpact(**impact_kw)
        self.executor = FakeExecutor(self.world)
        self.now = time.time()
        self.gk = Gatekeeper(state_dir=tmp / "gk", config=CFG, approver_pub=public_bytes(self.key),
                             impact=self.impact, snapshot_fn=self.world.snapshot,
                             executor_factory=lambda: self.executor, clock=lambda: self.now)

    def token(self, plan, action: str = "apply", key=None, ttl_min: int = 15, now: float | None = None, **over) -> str:
        p = make_payload(plan, action, "tester", ttl_min, now=self.now if now is None else now)
        p.update(over)
        return sign_token(key or self.key, p)
