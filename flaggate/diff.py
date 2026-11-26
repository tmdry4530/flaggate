"""Plan vs current state diff: machine form (list of changes) + human-readable text."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from flaggate.state import Snapshot, describe_rules

Kind = Literal["add", "change", "remove"]


class Change(BaseModel):
    kind: Kind
    path: str
    before: Any = None
    after: Any = None


def _fmt_weights(w: dict[str, float] | None) -> str:
    if not w:
        return "-"
    return ", ".join(f"{k} {v:g}%" for k, v in sorted(w.items()))


def diff_states(before: Snapshot, after: Snapshot) -> list[Change]:
    out: list[Change] = []
    for name in sorted(set(before.segments) | set(after.segments)):
        b, a = before.segments.get(name), after.segments.get(name)
        if b is None:
            out.append(Change(kind="add", path=f"segments/{name}", after=describe_rules(a.rules)))
        elif a is None:
            out.append(Change(kind="remove", path=f"segments/{name}", before=describe_rules(b.rules)))
        elif a.rules != b.rules:
            out.append(Change(kind="change", path=f"segments/{name}/rules",
                              before=describe_rules(b.rules), after=describe_rules(a.rules)))
    for name in sorted(set(before.features) | set(after.features)):
        b, a = before.features.get(name), after.features.get(name)
        base = f"features/{name}"
        if b is None:
            desc: dict[str, Any] = {"type": a.type, "enabled": a.env.enabled, "value": a.env.value}
            if a.env.weights:
                desc["weights"] = _fmt_weights(a.env.weights)
            out.append(Change(kind="add", path=base, after=desc))
            for seg, ov in sorted(a.overrides.items()):
                out.append(Change(kind="add", path=f"{base}/overrides/{seg}",
                                  after={"enabled": ov.enabled, "value": ov.value, "priority": ov.priority}))
            continue
        if a is None:  # features are never hard-deleted by plans
            out.append(Change(kind="remove", path=base, before={"type": b.type}))
            continue
        if a.archived != b.archived:
            out.append(Change(kind="change", path=f"{base}/archived", before=b.archived, after=a.archived))
        for v in sorted(set(a.variants) - set(b.variants)):
            out.append(Change(kind="add", path=f"{base}/variants/{v}", after={"default_weight": a.variants[v].default_weight}))
        if a.env.enabled != b.env.enabled:
            out.append(Change(kind="change", path=f"{base}/env/enabled", before=b.env.enabled, after=a.env.enabled))
        if a.env.value != b.env.value:
            out.append(Change(kind="change", path=f"{base}/env/value", before=b.env.value, after=a.env.value))
        if a.env.weights != b.env.weights:
            out.append(Change(kind="change", path=f"{base}/env/weights",
                              before=_fmt_weights(b.env.weights), after=_fmt_weights(a.env.weights)))
        for seg in sorted(set(a.overrides) | set(b.overrides)):
            bo, ao = b.overrides.get(seg), a.overrides.get(seg)
            p = f"{base}/overrides/{seg}"
            if bo is None:
                d = {"enabled": ao.enabled, "value": ao.value, "priority": ao.priority}
                if ao.weights:
                    d["weights"] = _fmt_weights(ao.weights)
                out.append(Change(kind="add", path=p, after=d))
            elif ao is None:
                out.append(Change(kind="remove", path=p, before={"enabled": bo.enabled, "value": bo.value}))
            else:
                bd = bo.model_dump(exclude={"segment_id"})
                ad = ao.model_dump(exclude={"segment_id"})
                if bd != ad:
                    out.append(Change(kind="change", path=p,
                                      before={k: v for k, v in bd.items() if ad[k] != v},
                                      after={k: v for k, v in ad.items() if bd[k] != v}))
    return out


SYMBOL = {"add": "+", "change": "~", "remove": "-"}


def _short(v: Any) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_short(x)}" for k, x in v.items()) + "}"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def render_text(changes: list[Change], color: bool = False) -> str:
    if not changes:
        return "  (no changes)"
    colors = {"add": "\033[32m", "change": "\033[33m", "remove": "\033[31m"}
    lines = []
    for c in changes:
        if c.kind == "add":
            body = f"{c.path}  {_short(c.after)}"
        elif c.kind == "remove":
            body = f"{c.path}  (was {_short(c.before)})"
        else:
            body = f"{c.path}  {_short(c.before)} -> {_short(c.after)}"
            if c.path.endswith("/archived") and c.after is True:
                body += "   [no effect on evaluation: Flagsmith keeps serving archived flags]"
        line = f"  {SYMBOL[c.kind]} {body}"
        lines.append(f"{colors[c.kind]}{line}\033[0m" if color else line)
    return "\n".join(lines)
