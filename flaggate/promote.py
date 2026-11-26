"""``fg promote``: pick an A/B winner from metrics and emit a normal plan (weights winner=100, others 0).

The plan goes through the same gate as any other: submit -> diff/impact -> human ``fg approve``.
The evidence (rates, z-test p-value, metrics file sha256) is written into the plan's ``prompt``, so it is part of
the hash the human signs.

metrics.csv formats (auto-detected from the header):
  aggregate:  variant,exposures,conversions            (optional leading ``feature`` column)
  events:     identity,variant,converted                (converted = 1/0/true/false; optional ``feature``)
"""
from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flaggate.plan import Plan
from flaggate.planner import plan_from_prompt
from flaggate.state import Snapshot, control_key


class PromoteError(ValueError):
    pass


@dataclass
class Analysis:
    feature: str
    control: str
    counts: dict[str, tuple[int, int]]          # variant -> (exposures, conversions)
    winner: str
    p_value: float | None
    significant: bool
    reasons: list[str] = field(default_factory=list)

    def rate(self, v: str) -> float:
        n, c = self.counts[v]
        return c / n if n else 0.0

    def summary(self) -> str:
        rates = ", ".join(f"{v} {self.rate(v):.1%} ({c}/{n})" for v, (n, c) in sorted(self.counts.items()))
        p = "n/a" if self.p_value is None else f"{self.p_value:.4f}"
        return f"{rates}; winner {self.winner} vs control {self.control}: p={p}"


def _truthy(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "converted")


def read_metrics(path: Path, feature: str | None = None) -> dict[str, tuple[int, int]]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise PromoteError(f"{path}: no rows")
    cols = set(rows[0])
    if feature and "feature" in cols:
        rows = [r for r in rows if r["feature"] == feature]
    counts: dict[str, list[int]] = {}
    if {"variant", "exposures", "conversions"} <= cols:
        for r in rows:
            c = counts.setdefault(r["variant"], [0, 0])
            c[0] += int(r["exposures"])
            c[1] += int(r["conversions"])
    elif {"variant", "converted"} <= cols:
        for r in rows:
            c = counts.setdefault(r["variant"], [0, 0])
            c[0] += 1
            c[1] += 1 if _truthy(r["converted"]) else 0
    else:
        raise PromoteError(f"{path}: need columns variant,exposures,conversions or identity,variant,converted")
    for v, (n, c) in counts.items():
        if c > n or n < 0 or c < 0:
            raise PromoteError(f"{path}: variant {v!r} has {c} conversions for {n} exposures")
    return {v: (n, c) for v, (n, c) in counts.items()}


def two_proportion_p(n1: int, c1: int, n2: int, c2: int) -> float | None:
    """Two-sided p-value of a pooled two-proportion z-test (normal approximation)."""
    if not n1 or not n2:
        return None
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (c1 / n1 - c2 / n2) / se
    return math.erfc(abs(z) / math.sqrt(2))


def analyze(snapshot: Snapshot, feature: str, counts: dict[str, tuple[int, int]],
            min_exposures: int = 100, alpha: float = 0.05) -> Analysis:
    if feature not in snapshot.features:
        raise PromoteError(f"unknown feature {feature!r}")
    f = snapshot.features[feature]
    if f.type != "MULTIVARIATE":
        raise PromoteError(f"{feature} is not a multivariate feature")
    control = control_key(f)
    known = {control, *f.variants}
    unknown = sorted(set(counts) - known)
    if unknown:
        raise PromoteError(f"metrics mention variants that {feature} does not have: {unknown}")
    counts = {v: counts.get(v, (0, 0)) for v in sorted(known)}
    reasons: list[str] = []
    eligible = {v: nc for v, nc in counts.items() if nc[0] >= min_exposures}
    if not eligible:
        raise PromoteError(f"no variant has >= {min_exposures} exposures yet")
    for v in sorted(set(counts) - set(eligible)):
        reasons.append(f"{v}: only {counts[v][0]} exposures (< {min_exposures}), not eligible")
    winner = max(eligible, key=lambda v: (eligible[v][1] / eligible[v][0], eligible[v][0]))
    if winner == control:
        best_other = max((v for v in eligible if v != control),
                         key=lambda v: eligible[v][1] / eligible[v][0], default=None)
        p = (two_proportion_p(*counts[control], *counts[best_other]) if best_other else None)
        reasons.append("control is the best performer")
    else:
        p = two_proportion_p(*counts[winner], *counts[control]) if counts[control][0] else None
        if p is None:
            reasons.append(f"control {control} has no exposures: significance unknown")
    significant = p is not None and p < alpha
    if not significant:
        reasons.append(f"difference not significant at alpha={alpha}")
    return Analysis(feature, control, counts, winner, p, significant, reasons)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def promote_plan(snapshot: Snapshot, analysis: Analysis, metrics_path: Path, *, force: bool = False,
                 author: str = "fg:promote") -> Plan:
    if not analysis.significant and not force:
        raise PromoteError("not promoting: " + "; ".join(analysis.reasons) + " (use --force to promote anyway)")
    plan = plan_from_prompt(f"promote {analysis.winner} for {analysis.feature}", snapshot, author=author)
    evidence = (f"promote {analysis.winner} for {analysis.feature} -- evidence: {analysis.summary()}"
                f"{' [FORCED]' if not analysis.significant else ''}; metrics sha256 "
                f"{file_sha256(metrics_path)[:16]} ({Path(metrics_path).name})")
    return plan.model_copy(update={"prompt": evidence})


def analysis_dict(a: Analysis) -> dict[str, Any]:
    return {"feature": a.feature, "control": a.control, "winner": a.winner, "p_value": a.p_value,
            "significant": a.significant, "rates": {v: a.rate(v) for v in a.counts},
            "counts": {v: {"exposures": n, "conversions": c} for v, (n, c) in a.counts.items()}, "notes": a.reasons}
