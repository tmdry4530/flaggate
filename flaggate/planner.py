"""Keyless rule-based planner v1: prompt -> Plan (no LLM, deterministic).

Understood clauses (case-insensitive; join several with ';', ',', '.', 'and', or new lines):

  roll out <feature> to <N>% of <word> users           -> segment plan==<word> AND N% split, override ON
  roll out <feature> to <N>% of users where <t>=<v>     -> segment <t>==<v> AND N% split, override ON
  roll out <feature> to <N>% of everyone|all users      -> segment N% split (100% -> just enable)
  enable <feature> for <word> users                     -> same as a 100% rollout to that audience
  a/b test <feature> <a> vs <b> [vs <c>] [p/q[/r]]      -> MV feature or weights (control gets the rest)
  a/b test <feature> <a> N%, <b> M% [and <c> K%]        -> same, weights per variant
  a/b test <feature> <a>/<b> p/q                        -> same
  split <feature> p/q between <a> and <b>               -> same ('a/b test ... between' too)
  enable|turn on <feature>   /   disable|turn off|kill <feature>
  set <feature> to <value>
  promote <variant> (for|on|in) <feature>               -> weights <variant>=100, others 0
  stop|end|remove rollout of <feature>                  -> delete its rollout overrides + segments
  archive <feature>
Modifiers (anywhere, may repeat if identical):
  max exposure N% | maximum exposure cap N% | enforce a N% maximum exposure cap
  | keeping (total) exposure under N% (of all users)    -> guardrails.max_exposure_pct
  target only <word> users | only for <word> users      -> audience of the rollout (must match 'of <...>')
'<feature>' may be written as 'the <feature> feature|flag', '<description> (feature <feature>)', or, in an A/B test,
as a description of an existing feature ('the button colors' -> checkout_button_color; must match exactly one).
Split percentages must sum to 100, except that an existing multivariate feature's control, when it is not listed,
gets the rest ('green vs orange 30/30' -> blue 40). Identical repeated requirements are merged; conflicting ones
(two caps, two audiences, two rollouts of one feature) raise an error naming both.

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
    parts = re.split(r"\s*(?:;|\n|,\s*(?:and\s+)?|\.\s+|\s+and\s+then\s+|\s+then\s+|\s+and\s+(?=(?:roll|a/?b|enable|disable|turn|kill|set|promote|stop|end|remove|archive|target|enforce|keep)\b))\s*",
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
        # Flagsmith rejects two segment overrides of a feature with the same priority ("Duplicate priority: 0"),
        # so a new override goes behind the existing ones; an existing one keeps its priority.
        others = [o.priority for name, o in st.features[feature].overrides.items() if name != seg]
        prio = ov.priority if ov is not None else (max(others) + 1 if others else 0)
        ctx.add(SetSegmentOverrideOp(op="set_segment_override", feature=feature, segment=target, enabled=True,
                                     value=st.features[feature].env.value if ov is None else ov.value, priority=prio))
    ctx.exposures.append(pct)


def _split(n: int, raw: str | None, rest_to: str | None = None) -> list[float]:
    """Weights for n listed variants. ``rest_to`` names the control when it is not listed (existing feature only):
    then a split below 100 leaves the rest to it; otherwise the split must sum to 100."""
    if raw:
        nums = [float(x) for x in raw.split("/")]
        if len(nums) != n:
            raise PlannerError(f"split {raw} has {len(nums)} parts for {n} variants")
        total = sum(nums)
        if total > 100 + 1e-6 or (abs(total - 100) > 1e-6 and rest_to is None):
            raise PlannerError(
                f"split {raw} sums to {total:g}%, not 100%"
                + ("" if total > 100 else " (only an existing multivariate feature's control, when it is not listed, "
                   "gets the rest; list every variant of a new feature, control first)"))
        return nums
    base = round(100 / n, 2)
    out = [base] * n
    out[-1] = round(100 - base * (n - 1), 2)
    return out


def _ab_test(ctx: _Ctx, feature: str, variants: list[str], split: str | None) -> None:
    st = ctx.state
    existing = st.features.get(feature)
    if len(set(variants)) != len(variants):
        raise PlannerError(f"a/b test {feature}: variant listed twice in {variants}")
    rest_to = None
    if existing is not None and existing.type == "MULTIVARIATE" and control_key(existing) not in variants:
        rest_to = control_key(existing)
    weights = _split(len(variants), split, rest_to)
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
    if split and rest_to is not None:
        target[control] = round(100.0 - sum(weights), 6)
    ctx.add(SetMvWeightsOp(op="set_mv_weights", feature=feature, weights=target))
    ctx.exposures.append(100.0 - target.get(control, 0.0))


def _need(ctx: _Ctx, feature: str) -> Any:
    st = ctx.state
    if feature not in st.features:
        raise PlannerError(f"unknown feature {feature!r}")
    return st.features[feature]


GRAMMAR_SHORT = (
    "One or more clauses joined by ';' ',' '.' or 'and': "
    "'roll out <feature> to <N>% of <word> users' (or 'of users where <trait>=<value>' / 'of everyone'), "
    "'enable <feature> for <word> users', 'a/b test <feature> <a> vs <b>' plus an optional split such as 50/50, 'enable|disable <feature>', "
    "'set <feature> to <value>', 'promote <variant> for <feature>', 'stop rollout of <feature>', "
    "'archive <feature>'. Modifiers: 'max exposure N%' (also 'keeping total exposure under N% of all users', "
    "'enforce a N% maximum exposure cap') and 'target only <word> users'. "
    "A/B forms: 'a/b test checkout_button_color green 30%, orange 30%, blue 40%', "
    "'a/b test the button color green/orange 50/50', 'split checkout_button_color 50/50 between green and orange' "
    "(splits sum to 100; an unlisted control of an existing feature gets the rest). Rollout and A/B in one prompt: "
    "'roll out new_checkout to 25% of pro users and a/b test checkout_button_color green/orange 50/50, keeping "
    "exposure under 20%'. "
    "Example: 'roll out new_checkout to 50% of pro users with max exposure 45%'."
)

_PCT = r"(\d+(?:\.\d+)?)\s*%"
_CAP_PATTERNS = [
    rf"(?:while\s+)?keep(?:ing)?\s+(?:the\s+)?(?:total\s+|overall\s+)?exposure\s+(?:under|below|within|at\s+most|<=?)\s+{_PCT}"
    rf"(?:\s+of\s+(?:all\s+)?(?:the\s+)?users)?",
    rf"(?:enforce|apply|set|use|with|add)\s+(?:an?\s+)?{_PCT}\s+(?:max(?:imum)?\s+)?(?:total\s+)?exposure(?:\s+(?:cap|limit))?",
    rf"(?:with\s+)?(?:an?\s+)?(?:the\s+)?max(?:imum)?\s+(?:total\s+)?exposure(?:\s+(?:cap|limit))?(?:\s+(?:of|at|=))?\s+{_PCT}",
    rf"(?:with\s+)?(?:an?\s+)?(?:total\s+)?exposure\s+(?:cap|limit)(?:\s+(?:of|at|=))?\s+{_PCT}",
    rf"(?:cap|limit)\s+(?:total\s+)?exposure\s+(?:at|to)\s+{_PCT}",
]
_WHO = r"(?:everyone|everybody|all\s+users|users\s+(?:where|with)\s+\w+\s*(?:=|==|is)\s*\S+|\w+\s+users)"
_TARGET_PATTERNS = [
    rf"(?:and\s+)?target(?:ing)?\s+only\s+(?:the\s+)?({_WHO})",
    rf"(?:and\s+)?(?:only\s+(?:for|to)|restrict(?:ed)?\s+to|limit(?:ed)?\s+to)\s+(?:the\s+)?({_WHO})",
]
_CONNECTIVES = r"(?:and|also|while|then|please|,|;)"


# ---- prompt normalisation (runs before clause splitting; rewrites, never deletes meaning) -------------------------
_N = r"(\d+(?:\.\d+)?)"
_KEYWORDS = (r"(?:roll|rollout|enable|disable|turn|kill|set|promote|stop|end|remove|undo|archive|keep|keeping|target|"
             r"targeting|enforce|with|max|maximum|cap|limit|only|while|then|also|and|vs|between|across|among|split|test)")
_W = rf"(?!{_KEYWORDS}\b)([A-Za-z][\w-]*)"
_NUMS = rf"[\[(]?\s*{_N}\s*%?(?:\s*/\s*{_N}\s*%?)+\s*[\])]?"   # '30/30/40', '[30/30/40]', '50%/50%'
_LISTWORDS = r"(?:vs\.?|and|between|across|among|with|using|at)"
_LSEP = r"(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)"
_PCT_ITEM = rf"{_W}\s*(?:at\s+|=\s*|:\s*|\(\s*)?{_N}\s*%\)?"
_END = r"(?![\w%/])"
_AB_SPECS = [  # (regex anchored at the variant spec, kind)
    (rf"(?:with\s+|using\s+)?{_PCT_ITEM}(?:{_LSEP}{_PCT_ITEM})+{_END}", "pct"),
    (rf"(?:with\s+|using\s+)?{_W}(?:\s*/\s*{_W})+(?:\s+(?:at\s+)?(?P<nums>{_NUMS}))?{_END}", "slash"),
    (rf"(?:with\s+|using\s+)?{_W}(?:\s+vs\.?\s+{_W})+(?:\s+(?:at\s+)?(?P<nums>{_NUMS}))?{_END}", "vs"),
    (rf"(?:(?P<nums>{_NUMS})\s+)?(?:between|across|among)\s+{_W}(?:{_LSEP}{_W})+(?:\s+(?P<nums2>{_NUMS}))?{_END}",
     "between"),
    (rf"{_W}(?:{_LSEP}{_W})+\s+(?P<nums>{_NUMS}){_END}", "list"),
]
_AB_HEAD = r"\b(?:a/?b(?:/?c)?[\s-]+test(?:ing)?|split[\s-]+test|split)\s+"
_NAME_VERBS = r"roll\s*out|rollout\s+of|test|split|enable|disable|turn\s+on|turn\s+off|kill|archive|set|stop|end"


def _norm_tokens(text: str) -> set[str]:
    out = set()
    for w in re.split(r"[\s_\-]+", text.lower()):
        w = w.replace("colour", "color")
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w and w not in ("the", "a", "an", "feature", "flag", "of", "for"):
            out.add(w)
    return out


def _describes(phrase: str, feature: str) -> bool:
    words = _norm_tokens(phrase)
    return bool(words) and words <= _norm_tokens(feature)


def _resolve_feature(phrase: str, snapshot: Snapshot) -> str | None:
    """A/B feature reference -> feature name, or None if the phrase is not a feature reference."""
    p = re.sub(r"\s*:\s*$", "", phrase.strip())
    p = re.sub(r"^(?:the\s+)?(?:(?:feature|flag)\s+)?", "", p, flags=re.I)
    p = re.sub(r"\s+(?:feature|flag)$", "", p, flags=re.I).strip()
    if not p:
        return None
    if re.fullmatch(NAME, p):
        return p  # a feature name (possibly new); plain words keep their old meaning
    if not re.fullmatch(r"[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*){1,3}", p):
        return None
    hits = sorted(f for f in snapshot.features if _describes(p, f))
    if len(hits) > 1:
        raise PlannerError(f"{phrase.strip()!r} matches several features: {', '.join(hits)}; name one")
    return hits[0] if hits else None


def _canonical_ab(m: re.Match, kind: str) -> tuple[list[str], str | None]:
    span = m.group(0)
    if kind == "pct":
        items = re.findall(_PCT_ITEM, span, flags=re.I)
        return [v for v, _ in items], "/".join(n for _, n in items)
    gd = m.groupdict()
    if gd.get("nums") and gd.get("nums2"):
        raise PlannerError(f"two splits in {span!r}")
    nums = gd.get("nums") or gd.get("nums2")
    rest = span
    for g in ("nums", "nums2"):
        if gd.get(g):
            rest = rest[: m.start(g) - m.start()] + " " + rest[m.end(g) - m.start():]
    # every variant word, in order (a repeated regex group would only keep the last one)
    words = [w for w in re.findall(r"[A-Za-z][\w-]*", rest) if not re.fullmatch(_LISTWORDS, w, re.I)]
    return words, (re.sub(r"[\s%\[\]()]", "", nums) if nums else None)


def _rewrite_ab(text: str, snapshot: Snapshot) -> str:
    """'a/b test the button color green 30%, orange 30%, blue 40%' -> 'a/b test checkout_button_color green vs
    orange vs blue 30/30/40' (one clause, no commas). Text that does not fit is left alone (and then rejected)."""
    pos = 0
    while h := re.compile(_AB_HEAD, re.I).search(text, pos):
        tail = text[h.end():]
        starts = [w.start() for w in re.finditer(r"\S+", tail)][:7]
        done = None
        for i, cut in enumerate(starts[1:] + [len(tail)] if starts else []):
            phrase = tail[:cut]
            feat = _resolve_feature(phrase, snapshot)
            if feat is None:
                continue
            for rx, kind in _AB_SPECS:
                if m := re.match(rx, tail[cut:], re.I):
                    variants, nums = _canonical_ab(m, kind)
                    done = (f"a/b test {feat} {' vs '.join(variants)}" + (f" {nums}" if nums else ""), cut + m.end())
                    break
            if done:
                break
        if done is None:
            pos = h.end()
            continue
        text = text[: h.start()] + done[0] + tail[done[1]:]
        pos = h.start() + len(done[0])
    return text


def _named_in_parens(text: str) -> str:
    """'roll out the new checkout (feature new_checkout)' -> 'roll out new_checkout' (the description must fit)."""
    def sub(m: re.Match) -> str:
        if m[2].strip() and not _describes(m[2], m[3]):
            raise PlannerError(f"{m[2].strip()!r} does not describe feature {m[3]!r} in {m[0]!r}")
        return f"{m[1]} {m[3]}"
    return re.sub(rf"\b({_NAME_VERBS})\s+(?:the\s+)?((?:[A-Za-z][\w-]*\s*){{0,5}}?)\s*"
                  rf"\(\s*(?:feature|flag)\s*[:=]?\s*({NAME})\s*\)", sub, text, flags=re.I)


def normalize_prompt(prompt: str, snapshot: Snapshot) -> str:
    text = re.sub(r"^\s*(?:product\s+|feature\s+|change\s+)?request\s*:\s*", "", prompt.strip(), flags=re.I)
    return _rewrite_ab(_named_in_parens(text), snapshot)


class _Intent:
    def __init__(self, kind: str, key: tuple, args: tuple, src: str):
        self.kind, self.key, self.args, self.src = kind, key, args, src


_VERBS = r"roll\s*out|enable|disable|archive|set|turn\s+on|turn\s+off|kill|test|rollout\s+of|(?:promote\s+\w+\s+)?(?:for|on|in)"


def _features_named(c: str) -> str:
    """'roll out the new_checkout feature' -> 'roll out new_checkout' (only right after a verb, never in values)."""
    return re.sub(rf"\b({_VERBS})\s+(?:the\s+)?({NAME})(?:\s+(?:feature|flag))?\b(?!\s*(?:feature|flag))",
                  lambda m: f"{m[1]} {m[2]}" if not re.fullmatch(r"the", m[2], re.I) else m[0], c, flags=re.I)


def _parse_clause(clause: str) -> list[_Intent]:
    """One clause -> intents (actions + modifiers). Raises PlannerError on any text it does not understand."""
    c = _features_named(clause.strip().rstrip(" ."))
    out: list[_Intent] = []
    for pat in _CAP_PATTERNS:
        while m := re.search(rf"\b{pat}", c, re.I):
            out.append(_Intent("cap", ("cap",), (float(m[1]),), m[0].strip()))
            c = (c[: m.start()] + " " + c[m.end():]).strip()
    for pat in _TARGET_PATTERNS:
        while m := re.search(rf"\b{pat}", c, re.I):
            slug, conds = _who(m[1])
            out.append(_Intent("target", ("target",), (slug, conds, m[1]), m[0].strip()))
            c = (c[: m.start()] + " " + c[m.end():]).strip()
    c = re.sub(rf"^(?:{_CONNECTIVES}\s*)+|(?:\s*{_CONNECTIVES})+$", "", c.strip(), flags=re.I).strip(" ,;")
    if not c:
        return out
    action = _parse_action(c)
    if action is None:
        where = f" in {clause.strip()!r}" if c != clause.strip().rstrip(" .") else ""
        raise PlannerError(f"could not understand {c!r}{where}. Supported:{HELP}")
    return [action, *out]


def _parse_action(c: str) -> _Intent | None:
    if m := re.fullmatch(rf"roll\s*out\s+({NAME})\s+to\s+{_PCT}(?:\s+of\s+(.+))?", c, re.I):
        pct = float(m[2])
        if m[3] and not re.fullmatch(r"(?:the\s+)?users", m[3].strip(), re.I):  # bare "users": audience comes from a modifier
            try:
                slug, conds = _who(m[3])
            except PlannerError:
                return None  # e.g. "pro users and make it sparkle": reported as not understood, never dropped
            return _Intent("rollout", ("rollout", m[1]), (m[1], pct, slug, conds, m[3]), m[0])
        return _Intent("rollout", ("rollout", m[1]), (m[1], pct, None, None, None), m[0])
    if m := re.fullmatch(rf"roll\s*out\s+({NAME})\s+to\s+(everyone|everybody|all users)", c, re.I):
        return _Intent("rollout", ("rollout", m[1]), (m[1], 100.0, "all", [], m[2]), m[0])
    if m := re.fullmatch(rf"(?:enable|turn\s+on)\s+({NAME})\s+(?:for|to)\s+(?:the\s+)?({_WHO})", c, re.I):
        slug, conds = _who(m[2])
        return _Intent("rollout", ("rollout", m[1]), (m[1], 100.0, slug, conds, m[2]), m[0])
    if m := re.fullmatch(rf"a/?b(?:/?c)?\s+test\s+({NAME})\s*:?\s+(\w+(?:\s+vs\.?\s+\w+)+)(?:\s+(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)+))?", c, re.I):
        variants = tuple(re.split(r"\s+vs\.?\s+", m[2], flags=re.I))
        return _Intent("ab", ("ab", m[1]), (m[1], variants, m[3]), m[0])
    if m := re.fullmatch(rf"(enable|turn\s+on|disable|turn\s+off|kill)\s+({NAME})", c, re.I):
        on = not re.fullmatch(r"disable|turn\s+off|kill", m[1], re.I)
        return _Intent("flag", ("flag", m[2]), (m[2], on), m[0])
    if m := re.fullmatch(rf"set\s+({NAME})\s+(?:to|=)\s+(.+)", c, re.I):
        return _Intent("value", ("value", m[1]), (m[1], _parse_value(m[2])), m[0])
    if m := re.fullmatch(rf"promote\s+(?:winner\s+)?(\w+)\s+(?:for|on|in)\s+({NAME})", c, re.I):
        return _Intent("promote", ("promote", m[2]), (m[2], m[1]), m[0])
    if m := re.fullmatch(rf"(?:stop|end|remove|undo)\s+(?:the\s+)?rollout\s+of\s+({NAME})", c, re.I):
        return _Intent("stop", ("stop", m[1]), (m[1],), m[0])
    if m := re.fullmatch(rf"archive\s+({NAME})", c, re.I):
        return _Intent("archive", ("archive", m[1]), (m[1],), m[0])
    return None


def _same(a: _Intent, b: _Intent) -> bool:
    if a.kind == "rollout":  # compare by meaning, not wording ("pro users" == "Pro users")
        return a.args[:3] == b.args[:3]
    if a.kind == "target":
        return a.args[0] == b.args[0]
    return a.args == b.args


def _describe(i: _Intent) -> str:
    if i.kind == "cap":
        return f"{i.src!r} ({i.args[0]:g}%)"
    if i.kind == "target":
        return f"{i.src!r} (audience {i.args[0]})"
    if i.kind == "rollout":
        return f"{i.src!r} ({i.args[1]:g}% of {i.args[2] or 'unspecified audience'})"
    return repr(i.src)


def _reconcile(intents: list[_Intent]) -> tuple[list[_Intent], float | None]:
    """Merge identical repeated requirements; raise on conflicting ones (naming both)."""
    caps = [i for i in intents if i.kind == "cap"]
    targets = [i for i in intents if i.kind == "target"]
    actions: list[_Intent] = []
    for i in (i for i in intents if i.kind not in ("cap", "target")):
        prev = next((a for a in actions if a.key == i.key), None)
        if prev is None:
            actions.append(i)
        elif not _same(prev, i):
            raise PlannerError(f"conflicting requirements: {_describe(prev)} vs {_describe(i)}")
    for a, b in zip(caps, caps[1:]):
        if a.args[0] != b.args[0]:
            raise PlannerError(f"conflicting exposure caps: {_describe(a)} vs {_describe(b)}")
    for a, b in zip(targets, targets[1:]):
        if not _same(a, b):
            raise PlannerError(f"conflicting audiences: {_describe(a)} vs {_describe(b)}")
    if targets:
        t = targets[0]
        rollouts = [a for a in actions if a.kind == "rollout"]
        if not rollouts:
            raise PlannerError(f"{t.src!r}: no rollout in the prompt to apply this audience to")
        for idx, a in enumerate(actions):
            if a.kind != "rollout":
                continue
            feat, pct, slug, conds, who = a.args
            if slug is None:
                actions[idx] = _Intent("rollout", a.key, (feat, pct, t.args[0], t.args[1], t.args[2]), a.src)
            elif slug != t.args[0]:
                raise PlannerError(f"conflicting audiences for {feat}: {_describe(a)} vs {_describe(t)}")
    return actions, (caps[0].args[0] if caps else None)


def _execute(ctx: _Ctx, i: _Intent) -> None:
    if i.kind == "rollout":
        feat, pct, slug, conds, who = i.args
        return _rollout(ctx, feat, pct, who or "everyone")
    if i.kind == "ab":
        return _ab_test(ctx, i.args[0], list(i.args[1]), i.args[2])
    if i.kind == "flag":
        feat, on = i.args
        f = _need(ctx, feat)
        return ctx.add(SetFlagOp(op="set_flag", feature=feat, enabled=on, value=f.env.value))
    if i.kind == "value":
        _need(ctx, i.args[0])
        return ctx.add(SetFlagOp(op="set_flag", feature=i.args[0], enabled=True, value=i.args[1]))
    if i.kind == "promote":
        feat, variant = i.args
        f = _need(ctx, feat)
        if f.type != "MULTIVARIATE" or variant not in f.env.weights:
            raise PlannerError(f"{variant!r} is not a variant of {feat!r} (variants: {sorted(f.env.weights)})")
        ctx.add(SetMvWeightsOp(op="set_mv_weights", feature=feat,
                               weights={k: (100.0 if k == variant else 0.0) for k in f.env.weights}))
        ctx.exposures.append(100.0)
        return
    if i.kind == "stop":
        feat = i.args[0]
        f = _need(ctx, feat)
        segs = sorted(s for s in f.overrides if s.startswith(f"{feat}_rollout_"))
        if not segs:
            raise PlannerError(f"{feat} has no FlagGate rollout overrides")
        for s in segs:
            ctx.add(DeleteSegmentOverrideOp(op="delete_segment_override", feature=feat, segment=s))
        for s in segs:
            ctx.add(DeleteSegmentOp(op="delete_segment", segment=s))
        return
    if i.kind == "archive":
        _need(ctx, i.args[0])
        return ctx.add(ArchiveFeatureOp(op="archive_feature", feature=i.args[0]))
    raise PlannerError(f"internal: unknown intent {i.kind}")  # pragma: no cover


def plan_from_prompt(prompt: str, snapshot: Snapshot, *, now: datetime | None = None,
                     author: str = "planner:rules", plan_id: str | None = None,
                     ttl_hours: int = 24) -> Plan:
    if not prompt.strip():
        raise PlannerError("empty prompt")
    ctx = _Ctx(snapshot)
    intents = [i for clause in split_clauses(normalize_prompt(prompt, snapshot)) for i in _parse_clause(clause)]
    actions, cap = _reconcile(intents)
    ctx.max_exposure = cap
    for i in actions:
        _execute(ctx, i)
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
