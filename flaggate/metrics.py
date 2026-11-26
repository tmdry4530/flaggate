"""'Saves real time' accounting (DESIGN §7): how many manual dashboard steps a plan replaces.

A *manual step* is one page/dialog a human would open and confirm in the Flagsmith dashboard to make the same
change by hand (estimates measured on the local v2.295.2 dashboard), plus one "who is affected?" check per plan
when an impact preview was produced (the dashboard cannot answer that at all; a human would sample identities).
"""
from __future__ import annotations

from typing import Any

from flaggate.plan import Plan

# op -> (base steps, extra steps per item); items = conditions for segments, variants for MV ops
STEPS: dict[str, tuple[int, str | None]] = {
    "create_feature": (3, None),            # Features page -> Create -> fill + save
    "create_mv_feature": (3, "variants"),   # + one row per variant
    "add_variant": (3, None),               # open feature -> Variations tab -> add + save
    "set_flag": (2, None),                  # open feature in environment -> toggle/value + confirm
    "set_mv_weights": (2, "weights"),       # open feature -> edit each weight -> save
    "create_segment": (2, "conditions"),    # Segments -> Create -> one row per condition -> save
    "update_segment_rules": (2, "conditions"),
    "set_segment_override": (3, None),      # feature -> Segment overrides -> pick segment, set, save
    "delete_segment_override": (2, None),
    "delete_segment": (2, None),
    "archive_feature": (2, None),
    "delete_feature": (2, None),
}


def _items(op: Any, kind: str | None) -> int:
    if kind == "conditions":
        def count(rules: list) -> int:
            return sum(len(r.conditions) + count(r.rules) for r in rules)
        return count(op.rules)
    if kind in ("variants", "weights"):
        return len(op.weights)
    return 0


def manual_steps(plan: Plan, impact_checked: bool = True) -> dict[str, Any]:
    by_op = []
    for op in plan.ops:
        base, kind = STEPS.get(op.op, (2, None))
        by_op.append((op.op, base + _items(op, kind)))
    dashboard = sum(n for _, n in by_op)
    total = dashboard + (1 if impact_checked else 0)
    return {"manual_steps_replaced": total, "dashboard_steps": dashboard,
            "impact_check": 1 if impact_checked else 0, "by_op": by_op}


def format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"
