"""Impact preview (DESIGN.md §5.3): replay a plan against real identities with Flagsmith's own flag engine.

    env document --apply plan ops (pure copy)--> after document
        |                                            |
        +-- flag_engine.get_evaluation_result --+----+   for every sampled identity
                                                |
                      who flips from what to what, per-segment aggregates, max exposure, guardrails

The output is a JSON-safe ``report`` (the §5.3 shape plus a few extra keys) and a human text rendering.
``ImpactResult.predictions()`` is the exact per-identity "after" state that :mod:`flaggate.verify` later
compares with the live SDK ("prediction matched N/N").

CLI::

    python -m flaggate.impact --plan plan.json --env-doc doc.json --identities ids.csv
    python -m flaggate.impact --plan plan.json --live --identities-live      # local self-host
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import metadata
from typing import Any, Iterable, Mapping, Sequence

from flag_engine.engine import get_evaluation_result
from flagsmith.mappers import map_context_and_identity_data_to_context, map_environment_document_to_context

from .docops import ApplyResult, apply_ops
from .identities import Identity, identities_from_records, load_identities
from .plan import Plan, plan_hash

KST = timezone(timedelta(hours=9))
ENV_DEFAULT = "(environment default)"
SPLIT = "(multivariate split)"
ABSENT = {"enabled": False, "value": None, "reason": "ABSENT"}


# --------------------------------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Evaluation:
    flags: Mapping[str, Mapping[str, Any]]   # feature -> {"enabled", "value", "reason"}
    segments: tuple[str, ...]                # names of the segments the identity matches


def norm_value(v: Any) -> Any:
    """None and '' both mean 'no value' in Flagsmith (a boolean flag has an empty value)."""
    if v is None or v == "":
        return None
    return v


def _engine_version() -> str:
    try:
        return f"flagsmith-flag-engine {metadata.version('flagsmith-flag-engine')}"
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "flagsmith-flag-engine"


def evaluate_identities(document: Mapping[str, Any], identities: Sequence[Identity]) -> dict[str, Evaluation]:
    """Evaluate every identity against ``document`` with flagsmith-flag-engine (same path as the SDK's
    local evaluation: ``map_environment_document_to_context`` + ``get_evaluation_result``)."""
    doc = dict(document)
    doc.setdefault("api_key", "fg-fixture-env-key")
    ctx = map_environment_document_to_context(doc)  # type: ignore[arg-type]
    out: dict[str, Evaluation] = {}
    for ident in identities:
        res = get_evaluation_result(
            map_context_and_identity_data_to_context(ctx, ident.identifier, dict(ident.traits)))
        out[ident.identifier] = Evaluation(
            flags={n: {"enabled": bool(f["enabled"]), "value": norm_value(f["value"]), "reason": f["reason"]}
                   for n, f in res["flags"].items()},
            segments=tuple(sorted(s["name"] for s in res["segments"])))
    return out


# --------------------------------------------------------------------------------------------------
# result type
# --------------------------------------------------------------------------------------------------
@dataclass
class ImpactResult:
    report: dict[str, Any]
    identities: list[Identity]
    before: dict[str, Evaluation] = field(default_factory=dict)
    after: dict[str, Evaluation] = field(default_factory=dict)
    after_document: dict[str, Any] | None = None
    plan: Plan | None = None
    before_document: Mapping[str, Any] | None = None

    @property
    def approvable(self) -> bool:
        return bool(self.report["approvable"])

    def predictions(self) -> dict[str, dict[str, dict[str, Any]]]:
        """identifier -> feature -> {"enabled", "value"} predicted after apply (what verify compares)."""
        return {i: {n: {"enabled": f["enabled"], "value": f["value"]} for n, f in ev.flags.items()}
                for i, ev in self.after.items()}

    def render(self, **kw: Any) -> str:
        return render_text(self.report, **kw)


# --------------------------------------------------------------------------------------------------
# core
# --------------------------------------------------------------------------------------------------
def _natural(s: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _state(ev: Evaluation, feature: str) -> Mapping[str, Any]:
    return ev.flags.get(feature, ABSENT)


def _segment_of(reason: str) -> str | None:
    if reason.startswith("TARGETING_MATCH") and "segment=" in reason:
        return reason.split("segment=", 1)[1].strip()
    return None


def _classify(before: Mapping[str, Any], after: Mapping[str, Any]) -> str | None:
    if before["enabled"] == after["enabled"] and before["value"] == after["value"]:
        return None
    if before["enabled"] != after["enabled"]:
        return "toggle"
    if str(before["reason"]).startswith("SPLIT") or str(after["reason"]).startswith("SPLIT"):
        return "variant"
    return "value"


def _attribution(kind: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    seg = _segment_of(str(after["reason"]))
    if seg:
        return seg
    if kind == "variant" or str(after["reason"]).startswith("SPLIT"):
        return SPLIT
    return ENV_DEFAULT


def _feature_counts(idents: Sequence[Identity], before: Mapping[str, Evaluation],
                    after: Mapping[str, Evaluation]) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for ident in idents:
        b, a = before[ident.identifier], after[ident.identifier]
        for feature in set(b.flags) | set(a.flags):
            kind = _classify(_state(b, feature), _state(a, feature))
            if kind is not None:
                out[feature]["variant" if kind == "variant" else "exposed"] += 1
    return out


def _id_sensitivity(env_document: Mapping[str, Any], view: Plan, idents: Sequence[Identity],
                    before: Mapping[str, Evaluation], applied: ApplyResult, hints: Mapping[str, Any] | None,
                    primary: Mapping[str, Counter], samples: int) -> dict[str, Any]:
    """PERCENTAGE_SPLIT membership hashes on the segment id, which the server assigns at apply time (a global
    database sequence: not predictable from the environment document). Re-run the after-evaluation with
    ``samples`` other plausible ids and report how far exposure can move, so the guardrail holds for the
    ids the server might pick and not only for the guessed one."""
    n = len(idents)
    exp = [max((c["exposed"] for c in primary.values()), default=0)]
    shift = [max((c["variant"] for c in primary.values()), default=0)]
    base_hints = dict(hints or {})
    for k in range(1, samples + 1):
        ids = dict(base_hints.get("segment_ids", {}))
        ids.update({name: applied.created_segment_ids[name] + k for name in applied.assumed_split_segments})
        alt = apply_ops(env_document, view, {**base_hints, "segment_ids": ids})
        if alt.errors:  # cannot happen when the primary applied cleanly, but never raise from a preview
            continue
        counts = _feature_counts(idents, before, evaluate_identities(alt.document, idents))
        exp.append(max((c["exposed"] for c in counts.values()), default=0))
        shift.append(max((c["variant"] for c in counts.values()), default=0))
    return {"segments": list(applied.assumed_split_segments), "candidates": len(exp),
            "exposure_pct": {"min": _pct(min(exp), n), "max": _pct(max(exp), n)},
            "variant_shift_pct": {"min": _pct(min(shift), n), "max": _pct(max(shift), n)}}


def _env_fingerprint(doc: Mapping[str, Any]) -> str:
    """sha256 of the document without the client key; for logs/audit only, not a security control."""
    stripped = {k: v for k, v in doc.items() if k != "api_key"}
    return "sha256:" + hashlib.sha256(json.dumps(stripped, sort_keys=True, default=str).encode()).hexdigest()


def compute_impact(
    env_document: Mapping[str, Any],
    plan: Plan | Mapping[str, Any],
    identities: Iterable[Any],
    *,
    hints: Mapping[str, Any] | None = None,
    max_variant_shift_pct: float | None = None,
    id_samples: int = 16,
    sample_source: str = "caller-supplied identities",
    fetched_at: datetime | None = None,
    max_listed: int = 10,
) -> ImpactResult:
    """Predict who would be served what, before vs after ``plan``. Never touches Flagsmith.

    ``plan`` is a :class:`flaggate.Plan` (a dict is validated into one). ``@ref`` segment/feature references
    are resolved here, in op order.

    Guardrails:
      * ``plan.guardrails.max_exposure_pct``: for every feature, the share of sampled identities whose
        *enabled state or plain value* changes must not exceed it. Exceeding => ``approvable: false``.
      * ``max_variant_shift_pct`` (optional keyword; not part of the plan schema): same for multivariate
        variant re-assignment (A/B weights). Experiments legitimately move a large share of users between
        variants, so this is only enforced if given; the shift is always reported.
    ``id_samples``: when the plan creates a segment with a PERCENTAGE_SPLIT and the server-assigned segment id is
    unknown (no ``hints``), exposure is also evaluated for that many other ids and the guardrails are checked
    against the worst case (``report["id_sensitivity"]``). 0 disables it.

    An empty sample, an op that cannot be applied, or any guardrail violation makes the plan not approvable.
    """
    view = plan if isinstance(plan, Plan) else Plan.model_validate(plan)
    idents = identities_from_records(identities)
    n = len(idents)
    fetched = fetched_at or datetime.now(KST)

    applied: ApplyResult = apply_ops(env_document, view, hints)
    blockers: list[str] = []
    warnings: list[str] = []
    if applied.errors:
        blockers += [f"plan cannot be applied: {e}" for e in applied.errors]
    if n == 0:
        blockers.append("no identities in the sample: exposure cannot be measured")

    before = evaluate_identities(env_document, idents) if n else {}
    after = evaluate_identities(applied.document, idents) if (n and not applied.errors) else {}

    flips_acc: dict[tuple, dict[str, Any]] = {}
    per_feature: dict[str, Counter] = defaultdict(Counter)       # feature -> {"exposed": x, "variant": y}
    seg_flips: Counter = Counter()
    changed_ids: set[str] = set()
    if after:
        for ident in idents:
            b, a = before[ident.identifier], after[ident.identifier]
            for feature in sorted(set(b.flags) | set(a.flags)):
                bs, as_ = _state(b, feature), _state(a, feature)
                kind = _classify(bs, as_)
                if kind is None:
                    continue
                changed_ids.add(ident.identifier)
                per_feature[feature]["variant" if kind == "variant" else "exposed"] += 1
                via = _attribution(kind, bs, as_)
                seg_flips[via] += 1
                key = (feature, kind, bs["enabled"], as_["enabled"], bs["value"], as_["value"])
                g = flips_acc.setdefault(key, {
                    "feature": feature, "kind": kind, "from": bs["enabled"], "to": as_["enabled"],
                    "value_from": bs["value"], "value_to": as_["value"], "count": 0,
                    "by_segment": Counter(), "identifiers": []})
                g["count"] += 1
                g["by_segment"][via] += 1
                g["identifiers"].append(ident.identifier)

    flips = []
    for g in sorted(flips_acc.values(), key=lambda g: (g["feature"], -g["count"], g["kind"])):
        ids = sorted(g.pop("identifiers"), key=_natural)
        flips.append({**g, "pct": _pct(g["count"], n), "by_segment": dict(sorted(g["by_segment"].items())),
                      "identifiers": ids[:max_listed], "identifiers_truncated": max(0, len(ids) - max_listed)})

    features = {f: {"exposed": c["exposed"], "exposure_pct": _pct(c["exposed"], n),
                    "variant_shifted": c["variant"], "variant_shift_pct": _pct(c["variant"], n)}
                for f, c in sorted(per_feature.items())}
    max_exp_feature, max_exp = max(((f, v["exposure_pct"]) for f, v in features.items()),
                                   key=lambda t: t[1], default=(None, 0.0))
    max_shift_feature, max_shift = max(((f, v["variant_shift_pct"]) for f, v in features.items()),
                                       key=lambda t: t[1], default=(None, 0.0))

    violations: list[str] = []
    if max_exp > view.guardrails.max_exposure_pct:
        violations.append(f"{max_exp_feature}: exposure {max_exp:g}% > max_exposure_pct {view.guardrails.max_exposure_pct:g}%")
    if max_variant_shift_pct is not None and max_shift > max_variant_shift_pct:
        violations.append(f"{max_shift_feature}: variant shift {max_shift:g}% > "
                          f"max_variant_shift_pct {max_variant_shift_pct:g}%")
    sens = None
    if after and id_samples > 0 and applied.assumed_split_segments:
        sens = _id_sensitivity(env_document, view, idents, before, applied, hints, per_feature, id_samples)
        if not violations and sens["exposure_pct"]["max"] > view.guardrails.max_exposure_pct:
            violations.append(
                f"exposure could reach {sens['exposure_pct']['max']:g}% > max_exposure_pct "
                f"{view.guardrails.max_exposure_pct:g}% depending on the server-assigned id of segment(s) "
                f"{', '.join(sens['segments'])}")
        if (max_variant_shift_pct is not None and not any("variant shift" in v for v in violations)
                and sens["variant_shift_pct"]["max"] > max_variant_shift_pct):
            violations.append(f"variant shift could reach {sens['variant_shift_pct']['max']:g}% > "
                              f"max_variant_shift_pct {max_variant_shift_pct:g}% depending on the server-assigned segment id")
    blockers += [f"guardrail: {v}" for v in violations]
    if after and not changed_ids and view.ops:
        warnings.append("the plan changes nothing for the sampled identities")

    # per-segment aggregates (membership before/after + flips attributed to the segment)
    seg_names = sorted({s for e in before.values() for s in e.segments} | {s for e in after.values() for s in e.segments})
    segments = []
    for name in seg_names:
        b_set = {i for i, e in before.items() if name in e.segments}
        a_set = {i for i, e in after.items() if name in e.segments} if after else set()
        segments.append({"segment": name, "members_before": len(b_set), "members_after": len(a_set),
                         "entered": len(a_set - b_set), "left": len(b_set - a_set),
                         "pct_after": _pct(len(a_set), n), "flips": seg_flips.get(name, 0)})
    other = {k: v for k, v in seg_flips.items() if k not in seg_names}

    approvable = not blockers
    report: dict[str, Any] = {
        "engine": _engine_version(),
        "env_document_at": fetched.isoformat(timespec="seconds"),
        "env_document_updated_at": env_document.get("updated_at"),
        "env_document_fp": _env_fingerprint(env_document),
        "plan_id": view.plan_id, "plan_hash": plan_hash(view),
        "sample": {"source": sample_source, "n": n},
        "evaluated": bool(after),
        "flips": flips,
        "features": features,
        "segments": segments,
        "flips_by_origin": dict(sorted(other.items())),
        "changed": len(changed_ids),
        "unchanged": n - len(changed_ids) if after else None,
        "max_exposure_pct": max_exp,
        "max_exposure_feature": max_exp_feature,
        "max_variant_shift_pct": max_shift,
        "guardrails": {"max_exposure_pct": view.guardrails.max_exposure_pct,
                       "max_variant_shift_pct": max_variant_shift_pct,
                       "violations": violations, "ok": not violations},
        "guardrails_ok": not violations,
        "approvable": approvable,
        "blockers": blockers,
        "warnings": warnings,
        "assumptions": applied.assumptions,
        "notes": applied.notes,
        "id_sensitivity": sens,
        "predicted_ids": {"segments": dict(applied.created_segment_ids)},
    }
    return ImpactResult(report=report, identities=idents, before=before, after=after,
                        after_document=applied.document if not applied.errors else None,
                        plan=view, before_document=env_document)


def attach_impact(plan: Plan, result: "ImpactResult | Mapping[str, Any]") -> Plan:
    """Return a copy of ``plan`` with the report stored in ``plan.impact``.

    ``impact`` is a non-semantic field, so ``plan_hash`` is unchanged (and a stored ``plan_hash`` stays
    valid). The plan itself is not modified.
    """
    report = result.report if isinstance(result, ImpactResult) else result
    return plan.model_copy(update={"impact": json.loads(json.dumps(report, default=str))})


# --------------------------------------------------------------------------------------------------
# text rendering
# --------------------------------------------------------------------------------------------------
def _fmt_value(v: Any) -> str:
    return "∅" if v is None else json.dumps(v)


def _describe_flip(f: Mapping[str, Any]) -> str:
    on = lambda b: "ON" if b else "OFF"  # noqa: E731
    parts = []
    if f["from"] != f["to"]:
        parts.append(f"{on(f['from'])} → {on(f['to'])}")
    if f["value_from"] != f["value_to"]:
        parts.append(f"value {_fmt_value(f['value_from'])} → {_fmt_value(f['value_to'])}")
    return ", ".join(parts)


def render_text(report: Mapping[str, Any], *, width: int = 78) -> str:
    n = report["sample"]["n"]
    lines = [f"IMPACT PREVIEW  plan {report['plan_id']}",
             f"engine: {report['engine']}   sample: {n} identities ({report['sample']['source']})",
             f"environment document fetched {report['env_document_at']}", ""]
    if not report["evaluated"]:
        lines.append("Could not evaluate the plan; no outcome preview available.")
    elif not report["flips"]:
        lines.append(f"WHO CHANGES: nobody ({n} of {n} identities see exactly what they see today)")
    else:
        lines.append(f"WHO CHANGES: {report['changed']} of {n} identities ({_pct(report['changed'], n):g}%); "
                     f"{report['unchanged']} unchanged")
        for f in report["flips"]:
            tag = {"toggle": "toggle", "value": "value", "variant": "variant"}[f["kind"]]
            via = ", ".join(f"{k}: {v}" for k, v in f["by_segment"].items())
            lines.append(f"  - {f['feature']}  {_describe_flip(f)}   {f['count']}/{n} ({f['pct']:g}%)  [{tag}]")
            lines.append(f"      via {via}")
            who = ", ".join(f["identifiers"])
            if f["identifiers_truncated"]:
                who += f", … (+{f['identifiers_truncated']} more)"
            lines.append(f"      e.g. {who}")
    if report["segments"]:
        lines += ["", "SEGMENTS"]
        for s in report["segments"]:
            lines.append(f"  - {s['segment']}: {s['members_before']} → {s['members_after']} members "
                         f"(+{s['entered']} / −{s['left']}), {s['pct_after']:g}% of sample, "
                         f"{s['flips']} flips attributed")
    for k, v in report["flips_by_origin"].items():
        lines.append(f"  - flips not tied to a segment, served by {k}: {v}")
    g = report["guardrails"]
    lines += ["", "GUARDRAILS"]
    if report["evaluated"]:
        mark = "OK" if report["max_exposure_pct"] <= g["max_exposure_pct"] else "EXCEEDED"
        lines.append(f"  max exposure {report['max_exposure_pct']:g}% "
                     f"{'≤' if mark == 'OK' else '>'} guardrail {g['max_exposure_pct']:g}%  "
                     f"[{mark}]" + (f"  ({report['max_exposure_feature']})" if report["max_exposure_feature"] else ""))
        if g["max_variant_shift_pct"] is not None:
            ok = report["max_variant_shift_pct"] <= g["max_variant_shift_pct"]
            lines.append(f"  max variant shift {report['max_variant_shift_pct']:g}% "
                         f"{'≤' if ok else '>'} limit {g['max_variant_shift_pct']:g}%  [{'OK' if ok else 'EXCEEDED'}]")
        elif report["max_variant_shift_pct"]:
            lines.append(f"  variant shift {report['max_variant_shift_pct']:g}% reported (no limit set)")
    sens = report.get("id_sensitivity")
    if sens:
        e = sens["exposure_pct"]
        lines.append(f"  segment id still unknown to the server: over {sens['candidates']} possible ids, exposure "
                     f"ranges {e['min']:g}%–{e['max']:g}% ({', '.join(sens['segments'])})")
    verdict = "APPROVABLE" if report["approvable"] else "NOT APPROVABLE"
    lines += ["", f"VERDICT: {verdict}"]
    lines += [f"  ✗ {b}" for b in report["blockers"]]
    if report["warnings"] or report["assumptions"] or report["notes"]:
        lines += ["", "NOTES"]
        lines += [f"  ! {w}" for w in report["warnings"]]
        lines += [f"  ~ assumption: {a}" for a in report["assumptions"]]
        lines += [f"  · {x}" for x in report["notes"]]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    from . import flagsmith_io as fio

    ap = argparse.ArgumentParser(prog="python -m flaggate.impact", description=__doc__.split("CLI::")[0].strip())
    ap.add_argument("--plan", required=True, help="plan JSON (flaggate.plan/v1)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--env-doc", help="environment document JSON file")
    src.add_argument("--live", action="store_true", help="fetch the environment document with FS_SERVER_KEY")
    ids = ap.add_mutually_exclusive_group(required=True)
    ids.add_argument("--identities", help="identities CSV/JSON file")
    ids.add_argument("--identities-live", action="store_true", help="fetch identities via the Admin API")
    ap.add_argument("--limit", type=int, default=500, help="max identities fetched with --identities-live")
    ap.add_argument("--max-variant-shift-pct", type=float, default=None,
                    help="optional limit on multivariate variant re-assignment (not in the plan schema)")
    ap.add_argument("--json", action="store_true", help="print the JSON report instead of text")
    args = ap.parse_args(argv)

    try:
        with open(args.plan) as fh:
            plan = Plan.model_validate_json(fh.read())
        creds = fio.load_credentials() if (args.live or args.identities_live) else {}
        api_url = fio.api_url_from_env()
        if args.live:
            doc = fio.fetch_environment_document(api_url, creds["FS_SERVER_KEY"])
        else:
            with open(args.env_doc) as fh:
                doc = json.load(fh)
        if args.identities_live:
            idents = fio.fetch_identities(api_url, creds["FS_ADMIN_API_KEY"], creds["FS_ENV_KEY"], limit=args.limit)
            source = "flagsmith identities API"
        else:
            idents = load_identities(args.identities)
            source = args.identities
    except KeyError as exc:
        print(f"missing credential {exc.args[0]} (set it in the environment or the env file)", file=sys.stderr)
        return 2
    except (OSError, ValueError, fio.FlagsmithHTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = compute_impact(doc, plan, idents, sample_source=source,
                                max_variant_shift_pct=args.max_variant_shift_pct)
    except ValueError as exc:  # pydantic.ValidationError
        print(f"invalid plan: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.report, indent=2, default=str) if args.json else result.render())
    return 0 if result.approvable else 1


if __name__ == "__main__":
    raise SystemExit(main())
