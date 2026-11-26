"""Keyless rule-based planner v1: prompt -> Plan (no LLM, deterministic).

Understood clauses (case-insensitive; join several with ';', ',', 'and', or new lines):

  roll out <feature> to <N>% of <word> users           -> segment plan==<word> AND N% split, override ON
  roll out <feature> to <N>% of users where <t>=<v>     -> segment <t>==<v> AND N% split, override ON
  roll out <feature> to <N>% of everyone|all users      -> segment N% split (100% -> just enable)
  a/b test <feature> <a> vs <b> [vs <c>] [p/q[/r]]      -> MV feature or weights (control gets the rest)
  enable|turn on <feature>   /   disable|turn off|kill <feature>
  set <feature> to <value>
  promote <variant> (for|on|in) <feature>               -> weights <variant>=100, others 0
  stop|end|remove rollout of <feature>                  -> delete its rollout overrides + segments
  archive <feature>
  ... with max exposure <N>% | keeping exposure under <N>%  -> guardrails.max_exposure_pct

Unknown clauses raise PlannerError listing what is supported. Rollback ops are left
empty here; the gatekeeper generates them from the live snapshot on submit.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from flaggate.plan import (
    AddVariantOp,
    ArchiveFeatureOp,
    CreateFeatureOp,
    CreateMvFeatureOp,
    CreateSegmentOp,
    DeleteSegmentOp,
    DeleteSegmentOverrideOp,
    Guardrails,
    Op,
    Plan,
    Rollback,
    SegmentCondition,
    SegmentRule,
    SetFlagOp,
    SetMvWeightsOp,
    SetSegmentOverrideOp,
    UpdateSegmentRulesOp,
)
from flaggate.state import Snapshot, control_key, normalize_rules, sha256_hex, simulate, state_hash

NAME = r"[A-Za-z][\w-]*"
HELP = __doc__.split("Understood clauses", 1)[1].split("Unknown clauses", 1)[0]


class PlannerError(ValueError):
    pass


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "all"


def _parse_value(raw: str) -> str | int | bool:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def split_clauses(prompt: str) -> list[str]:
    parts = re.split(r"\s*(?:;|\n|,\s*(?:and\s+)?|\.\s+|\s+and\s+then\s+|\s+then\s+|\s+and\s+(?=(?:roll|a/?b|enable|disable|turn|kill|set|promote|stop|end|remove|archive)\b))\s*",
                     prompt.strip(), flags=re.I)
    return [p.strip().rstrip(".") for p in parts if p and p.strip().rstrip(".")]


class _Ctx:
    def __init__(self, snapshot: Snapshot):
        self.base = snapshot
        self.ops: list[Op] = []
        self.max_exposure: float | None = None
        self.exposures: list[float] = []

    @property
    def state(self) -> Snapshot:
        return simulate(self.base, self.ops)

    def add(self, *ops: Op) -> None:
        self.ops.extend(ops)


def _who(text: str) -> tuple[str, list[SegmentCondition]]:
    text = text.strip()
    if re.fullmatch(r"(everyone|everybody|all(\s+users)?|users)", text, re.I):
        return "all", []
    m = re.fullmatch(r"users\s+(?:where|with)\s+(\w+)\s*(?:=|==|is)\s*(\S+)", text, re.I)
    if m:
        return f"{m[1]}_{m[2]}", [SegmentCondition(property=m[1], operator="EQUAL", value=m[2])]
    m = re.fullmatch(r"(\w+)\s+users", text, re.I)
    if m:
        return m[1].lower(), [SegmentCondition(property="plan", operator="EQUAL", value=m[1].lower())]
    raise PlannerError(f"don't know who {text!r} is (try 'pro users', 'everyone', 'users where country=KR')")


def _rollout(ctx: _Ctx, feature: str, pct: float, who: str) -> None:
    if not 0 < pct <= 100:
        raise PlannerError("rollout percentage must be in (0, 100]")
    st = ctx.state
    slug, conds = _who(who)
    if feature not in st.features:
        ctx.add(CreateFeatureOp(op="create_feature", name=feature, default_enabled=False,
                                description=f"created by FlagGate planner"))
        st = ctx.state
    if not conds and pct >= 100:
        f = st.features[feature]
        ctx.add(SetFlagOp(op="set_flag", feature=feature, enabled=True, value=f.env.value))
        ctx.exposures.append(100.0)
        return
    if pct < 100:
        conds = conds + [SegmentCondition(property="", operator="PERCENTAGE_SPLIT", value=f"{pct:g}")]
    rules = [SegmentRule(type="ALL", rules=[], conditions=conds)]
    seg = f"{feature}_rollout_{slug}"
    if seg in st.segments:
        if st.segments[seg].rules != normalize_rules(rules):
            ctx.add(UpdateSegmentRulesOp(op="update_segment_rules", segment=seg, rules=rules))
        target = seg
    else:
        ref = f"seg_{_slug(seg)}"
        ctx.add(CreateSegmentOp(op="create_segment", ref=ref, name=seg, rules=rules,
                                description=f"FlagGate rollout of {feature}: {pct:g}% of {who}"))
        target = f"@{ref}"
    ov = st.features[feature].overrides.get(seg)
    if ov is None or not ov.enabled:
        ctx.add(SetSegmentOverrideOp(op="set_segment_override", feature=feature, segment=target, enabled=True,
                                     value=st.features[feature].env.value if ov is None else ov.value, priority=0))
    ctx.exposures.append(pct)


def _split(n: int, raw: str | None) -> list[float]:
    if raw:
        nums = [float(x) for x in raw.split("/")]
        if len(nums) != n:
            raise PlannerError(f"split {raw} has {len(nums)} parts for {n} variants")
        if abs(sum(nums) - 100) > 1e-6:
            raise PlannerError(f"split {raw} does not sum to 100")
        return nums
    base = round(100 / n, 2)
    out = [base] * n
    out[-1] = round(100 - base * (n - 1), 2)
    return out


def _ab_test(ctx: _Ctx, feature: str, variants: list[str], split: str | None) -> None:
    weights = _split(len(variants), split)
    st = ctx.state
    if feature not in st.features:
        control = variants[0]
        ctx.add(CreateMvFeatureOp(op="create_mv_feature", name=feature, default_enabled=True, initial_value=control,
                                  weights=dict(zip(variants, weights)), description="A/B test created by FlagGate"))
        ctx.exposures.append(100.0 - weights[0])
        return
    f = st.features[feature]
    if f.type != "MULTIVARIATE":
        raise PlannerError(f"{feature} exists but is not multivariate; pick a new name for the experiment")
    control = control_key(f)
    for v in variants:
        if v != control and v not in f.variants:
            ctx.add(AddVariantOp(op="add_variant", feature=feature, value=v, weight=0))
    st = ctx.state
    target = {k: 0.0 for k in st.features[feature].env.weights}
    for v, w in zip(variants, weights):
        target[v] = w
    ctx.add(SetMvWeightsOp(op="set_mv_weights", feature=feature, weights=target))
    ctx.exposures.append(100.0 - target.get(control, 0.0))


def _need(ctx: _Ctx, feature: str) -> Any:
    st = ctx.state
    if feature not in st.features:
        raise PlannerError(f"unknown feature {feature!r}")
    return st.features[feature]


def _clause(ctx: _Ctx, clause: str) -> None:
    c = clause.strip()
    c = c.rstrip(" .")
    c = re.sub(rf"\b(?:the\s+)?({NAME})\s+(?:feature|flag)\b", r"\1", c, flags=re.I)  # "the X feature" -> "X"
    m = re.search(r"\b(?:with\s+)?max(?:imum)?\s+exposure\s+(\d+(?:\.\d+)?)\s*%", c, re.I) or re.search(
        r"\b(?:(?:while\s+)?keep(?:ing)?|with)\s+(?:the\s+)?(?:total\s+|overall\s+)?exposure\s+(?:under|below|at\s+most|<=?|of\s+at\s+most)\s+"
        r"(\d+(?:\.\d+)?)\s*%(?:\s+of\s+(?:all\s+)?users)?", c, re.I)
    if m:
        ctx.max_exposure = float(m[1])
        c = (c[: m.start()] + c[m.end():]).strip(" ,")
        if not c:
            return
    if m := re.fullmatch(rf"roll\s*out\s+({NAME})\s+to\s+(\d+(?:\.\d+)?)\s*%\s+of\s+(.+)", c, re.I):
        return _rollout(ctx, m[1], float(m[2]), m[3])
    if m := re.fullmatch(rf"roll\s*out\s+({NAME})\s+to\s+(everyone|all users)", c, re.I):
        return _rollout(ctx, m[1], 100, m[2])
    if m := re.fullmatch(rf"a/?b(?:/?c)?\s+test\s+({NAME})\s*:?\s+(\w+(?:\s+vs\.?\s+\w+)+)(?:\s+(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)+))?", c, re.I):
        return _ab_test(ctx, m[1], re.split(r"\s+vs\.?\s+", m[2], flags=re.I), m[3])
    if m := re.fullmatch(rf"(enable|turn\s+on|disable|turn\s+off|kill)\s+({NAME})", c, re.I):
        f = _need(ctx, m[2])
        on = not re.fullmatch(r"disable|turn\s+off|kill", m[1], re.I)
        return ctx.add(SetFlagOp(op="set_flag", feature=m[2], enabled=on, value=f.env.value))
    if m := re.fullmatch(rf"set\s+({NAME})\s+(?:to|=)\s+(.+)", c, re.I):
        _need(ctx, m[1])
        return ctx.add(SetFlagOp(op="set_flag", feature=m[1], enabled=True, value=_parse_value(m[2])))
    if m := re.fullmatch(rf"promote\s+(?:winner\s+)?(\w+)\s+(?:for|on|in)\s+({NAME})", c, re.I):
        f = _need(ctx, m[2])
        if f.type != "MULTIVARIATE" or m[1] not in f.env.weights:
            raise PlannerError(f"{m[1]!r} is not a variant of {m[2]!r} (variants: {sorted(f.env.weights)})")
        ctx.add(SetMvWeightsOp(op="set_mv_weights", feature=m[2],
                               weights={k: (100.0 if k == m[1] else 0.0) for k in f.env.weights}))
        ctx.exposures.append(100.0)
        return
    if m := re.fullmatch(rf"(?:stop|end|remove|undo)\s+(?:the\s+)?rollout\s+of\s+({NAME})", c, re.I):
        f = _need(ctx, m[1])
        segs = sorted(s for s in f.overrides if s.startswith(f"{m[1]}_rollout_"))
        if not segs:
            raise PlannerError(f"{m[1]} has no FlagGate rollout overrides")
        for s in segs:
            ctx.add(DeleteSegmentOverrideOp(op="delete_segment_override", feature=m[1], segment=s))
        for s in segs:
            ctx.add(DeleteSegmentOp(op="delete_segment", segment=s))
        return
    if m := re.fullmatch(rf"archive\s+({NAME})", c, re.I):
        _need(ctx, m[1])
        return ctx.add(ArchiveFeatureOp(op="archive_feature", feature=m[1]))
    raise PlannerError(f"could not understand {clause!r}. Supported:{HELP}")


def plan_from_prompt(prompt: str, snapshot: Snapshot, *, now: datetime | None = None,
                     author: str = "planner:rules", plan_id: str | None = None,
                     ttl_hours: int = 24) -> Plan:
    if not prompt.strip():
        raise PlannerError("empty prompt")
    ctx = _Ctx(snapshot)
    for clause in split_clauses(prompt):
        _clause(ctx, clause)
    if not ctx.ops:
        raise PlannerError("nothing to do: the requested state is already live")
    now = now or datetime.now(timezone.utc).astimezone()
    exposure = max(ctx.exposures) if ctx.exposures else 0.0
    guard = ctx.max_exposure if ctx.max_exposure is not None else max(20.0, exposure)
    pid = plan_id or f"pl_{now:%Y%m%d}_{sha256_hex(prompt + now.isoformat())[:4]}"
    return Plan(
        plan_id=pid, created_at=now, author=author, prompt=prompt.strip(), target=snapshot.target,
        base_state_hash=state_hash(snapshot), ops=ctx.ops, rollback=Rollback(ops=[]),
        guardrails=Guardrails(max_exposure_pct=guard, token_ttl_min=15),
        expires_at=now + timedelta(hours=ttl_hours),
    )
