"""Post-apply verification: compare what the live Flagsmith serves with what the impact preview predicted.

    prediction (ImpactResult.predictions())      live SDK (flagsmith==6.3.0, remote evaluation)
    identity -> feature -> {enabled, value}  vs  get_identity_flags(identity, traits).all_flags()

Result line (also stored as ``prediction_match`` in the audit record, DESIGN.md §5.4)::

    prediction matched 200/200

An identity matches only if *every* predicted feature has the predicted enabled state and value. Errors
talking to Flagsmith count as mismatches (fail closed). On a mismatch, if the live environment document is
available, the verifier says whether the flag engine run on the *live* document agrees with the SDK and
whether server-assigned segment ids differ from the ones assumed in the preview (percentage splits hash on
them), so the human sees the likely cause instead of just "mismatch".

CLI (after applying a plan to the local self-host)::

    python -m flaggate.verify --plan plan.json --before-env-doc before.json --identities ids.csv --live
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .identities import Identity, identities_from_records, load_identities
from .plan import Plan
from .impact import ImpactResult, compute_impact, evaluate_identities, norm_value

LiveEval = Callable[[Identity], Mapping[str, Mapping[str, Any]]]


@dataclass
class Mismatch:
    identifier: str
    feature: str | None
    predicted: Mapping[str, Any] | None
    live: Mapping[str, Any] | None
    error: str | None = None


@dataclass
class VerifyResult:
    matched: int
    total: int
    mismatches: list[Mismatch] = field(default_factory=list)
    diagnosis: list[str] = field(default_factory=list)
    # Same comparison, but with the preview re-run using the server-assigned ids (only set on a mismatch
    # when the live environment document was available and the plan created segments).
    reprediction: "VerifyResult | None" = None

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.matched == self.total

    @property
    def prediction_match(self) -> str:
        return f"{self.matched}/{self.total}"

    def summary(self) -> str:
        return f"prediction matched {self.matched}/{self.total}"

    def render(self, max_lines: int = 10) -> str:
        lines = [self.summary()]
        if self.total == 0:
            lines.append("  ✗ nothing to verify (no identities)")
        shown = 0
        seen: set[str] = set()
        for m in self.mismatches:
            if m.identifier in seen:
                continue
            seen.add(m.identifier)
            if shown >= max_lines:
                lines.append(f"  … and {len({x.identifier for x in self.mismatches}) - shown} more identities")
                break
            if m.error:
                lines.append(f"  ✗ {m.identifier}: {m.error}")
            else:
                lines.append(f"  ✗ {m.identifier}: {m.feature} predicted {_fmt(m.predicted)}, live {_fmt(m.live)}")
            shown += 1
        lines += [f"  ? {d}" for d in self.diagnosis]
        if self.reprediction is not None:
            lines.append(f"  with server-assigned ids the preview would have been: {self.reprediction.summary()}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"prediction_match": self.prediction_match, "ok": self.ok,
                "mismatches": [vars(m) for m in self.mismatches], "diagnosis": self.diagnosis,
                "reprediction_match": self.reprediction.prediction_match if self.reprediction else None}


def _fmt(state: Mapping[str, Any] | None) -> str:
    if state is None:
        return "<absent>"
    return f"{'ON' if state['enabled'] else 'OFF'}/{json.dumps(state['value'])}"


def _same(pred: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    if bool(pred["enabled"]) != bool(live["enabled"]):
        return False
    a, b = norm_value(pred["value"]), norm_value(live["value"])
    if a == b:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).lower() == str(b).lower()
    return str(a) == str(b)


def compare_predictions(predictions: Mapping[str, Mapping[str, Mapping[str, Any]]],
                        identities: Sequence[Identity], live_eval: LiveEval) -> VerifyResult:
    """Core comparison; ``live_eval`` is injectable so it is testable without a server."""
    matched = 0
    mismatches: list[Mismatch] = []
    for ident in identities:
        pred = predictions.get(ident.identifier)
        if pred is None:
            mismatches.append(Mismatch(ident.identifier, None, None, None, "no prediction for this identity"))
            continue
        try:
            live = live_eval(ident)
        except Exception as exc:  # fail closed, but never leak URLs/keys from the exception text
            mismatches.append(Mismatch(ident.identifier, None, None, None,
                                       f"live evaluation failed ({type(exc).__name__})"))
            continue
        bad = []
        for feature, p in pred.items():
            lv = live.get(feature, {"enabled": False, "value": None})
            if not _same(p, lv):
                bad.append(Mismatch(ident.identifier, feature, p, lv))
        if bad:
            mismatches += bad
        else:
            matched += 1
    return VerifyResult(matched=matched, total=len(identities), mismatches=mismatches)


def sdk_live_eval(api_url: str, environment_key: str, *, transient: bool = True,
                  timeout: float = 15.0) -> LiveEval:
    """Live evaluation through the official Python SDK (flagsmith 6.3.0, remote evaluation).

    ``environment_key`` is the environment's client-side key. ``transient=True`` (the default) evaluates
    with the given traits for this call only and does not store them on the identity. Pass
    ``transient=False`` only when the caller *intends* to persist traits (e.g. seeding a fixture).
    Verification always uses the default.
    """
    from flagsmith import Flagsmith

    base = api_url.rstrip("/")
    client = Flagsmith(environment_key=environment_key,
                       api_url=base if base.endswith("/api/v1") else base + "/api/v1/",
                       request_timeout_seconds=timeout, enable_analytics=False)

    def evaluate(ident: Identity) -> dict[str, dict[str, Any]]:
        flags = client.get_identity_flags(ident.identifier, traits=dict(ident.traits), transient=transient)
        return {f.feature_name: {"enabled": bool(f.enabled), "value": f.value} for f in flags.all_flags()}

    return evaluate


def sdk_offline_eval(document: Mapping[str, Any]) -> LiveEval:
    """The official SDK in offline mode over an environment document: an SDK-level (not just engine-level)
    evaluation that needs no server. Handy for tests and for dry-run verification."""
    import tempfile
    from pathlib import Path

    from flagsmith import Flagsmith
    from flagsmith.offline_handlers import LocalFileHandler

    doc = dict(document)
    doc.setdefault("api_key", "fg-fixture-env-key")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "env.json"
        path.write_text(json.dumps(doc))
        handler = LocalFileHandler(str(path))
    client = Flagsmith(offline_mode=True, offline_handler=handler)

    def evaluate(ident: Identity) -> dict[str, dict[str, Any]]:
        flags = client.get_identity_flags(ident.identifier, traits=dict(ident.traits))
        return {f.feature_name: {"enabled": bool(f.enabled), "value": f.value} for f in flags.all_flags()}

    return evaluate


def hints_from_live_document(impact: ImpactResult, live_document: Mapping[str, Any]) -> dict[str, Any]:
    """Real segment ids of the segments the plan created: feed back as ``hints=`` to compute_impact."""
    ids = {s["name"]: s["id"] for s in live_document["project"]["segments"]}
    return {"segment_ids": {n: ids[n] for n in impact.report.get("predicted_ids", {}).get("segments", {}) if n in ids}}


def repredict_with_live_ids(impact: ImpactResult, live_document: Mapping[str, Any]) -> ImpactResult:
    """Re-run the preview on the original (pre-apply) document with the real, server-assigned segment ids.

    Intended for a two-phase apply: create the segments first (they change nothing until an override exists),
    read the live document, call this, check the numbers/guardrails are still acceptable, then apply the rest and
    verify against *this* result. Raises ValueError if ``impact`` has no plan/document (not produced by compute_impact).
    """
    if impact.plan is None or impact.before_document is None:
        raise ValueError("impact was not produced by compute_impact")
    return compute_impact(impact.before_document, impact.plan, impact.identities,
                          hints=hints_from_live_document(impact, live_document),
                          sample_source=impact.report["sample"]["source"])


def diagnose(impact: ImpactResult, result: VerifyResult, live_document: Mapping[str, Any],
             live_eval_results: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None) -> list[str]:
    out: list[str] = []
    live_ids = {s["name"]: s["id"] for s in live_document["project"]["segments"]}
    for name, assumed in impact.report.get("predicted_ids", {}).get("segments", {}).items():
        actual = live_ids.get(name)
        if actual is None:
            out.append(f"segment {name!r} is not present in the live environment document (plan not applied?)")
        elif actual != assumed:
            out.append(f"segment {name!r} got id {actual} but the preview assumed {assumed}; "
                       f"PERCENTAGE_SPLIT membership depends on it (re-run the preview with the real id)")
    bad = sorted({m.identifier for m in result.mismatches if not m.error})
    if bad:
        idents = [i for i in impact.identities if i.identifier in set(bad)]
        engine_live = {i: {n: {"enabled": f["enabled"], "value": f["value"]} for n, f in ev.flags.items()}
                       for i, ev in evaluate_identities(live_document, idents).items()}
        by_id = {m.identifier: m for m in result.mismatches}
        agree = sum(1 for i in bad
                    if all(_same(engine_live[i].get(m.feature, {"enabled": False, "value": None}), m.live)
                           for m in result.mismatches if m.identifier == i and m.feature))
        out.append(f"flag engine on the live environment document agrees with the live SDK for "
                   f"{agree}/{len(by_id)} mismatched identities"
                   + ("; the plan was applied differently than predicted" if agree == len(by_id)
                      else "; the live server evaluates differently from the local engine"))
    return out


def _prefill(identities: Sequence[Identity], cached: Callable[[Identity], Any], workers: int = 8) -> None:
    """Warm the cache with concurrent live evaluations (one HTTP call per identity). Errors are left for the
    sequential pass in compare_predictions to raise or report exactly as before."""
    from concurrent.futures import ThreadPoolExecutor

    def one(ident: Identity) -> None:
        try:
            cached(ident)
        except Exception:  # noqa: BLE001 - re-raised/handled by the sequential comparison
            pass

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, identities))


def verify_prediction(impact: ImpactResult, live_eval: LiveEval, *,
                      live_document: Mapping[str, Any] | None = None) -> VerifyResult:
    """Compare ``impact.predictions()`` with ``live_eval`` for every sampled identity."""
    if not impact.after:
        return VerifyResult(0, len(impact.identities), diagnosis=["the preview could not be evaluated"])
    cache: dict[str, Mapping[str, Mapping[str, Any]]] = {}

    def cached(ident: Identity) -> Mapping[str, Mapping[str, Any]]:
        if ident.identifier not in cache:
            cache[ident.identifier] = live_eval(ident)
        return cache[ident.identifier]

    _prefill(impact.identities, cached)
    result = compare_predictions(impact.predictions(), impact.identities, cached)
    if result.mismatches and live_document is not None:
        result.diagnosis = diagnose(impact, result, live_document)
        hints = hints_from_live_document(impact, live_document)
        if hints["segment_ids"] and impact.plan is not None and impact.before_document is not None:
            again = compute_impact(impact.before_document, impact.plan, impact.identities, hints=hints)
            if again.after:
                result.reprediction = compare_predictions(again.predictions(), impact.identities, cached)
    return result


def verify_live(impact: ImpactResult, *, api_url: str, environment_key: str, server_key: str | None = None,
                transient: bool = True) -> VerifyResult:
    """Convenience: SDK remote evaluation against ``api_url``; fetches the live environment document for
    diagnosis only when something mismatched and ``server_key`` is given.

    Uses ``transient=True`` so verification never stores identity traits. Pass ``transient=False`` only
    if the caller intentionally wants the SDK to persist them (not the verification path).
    """
    from . import flagsmith_io as fio

    live_eval = sdk_live_eval(api_url, environment_key, transient=transient)
    result = verify_prediction(impact, live_eval)
    if result.mismatches and server_key:
        try:
            doc = fio.fetch_environment_document(api_url, server_key)
        except fio.FlagsmithHTTPError as exc:
            result.diagnosis = [f"could not fetch the live environment document for diagnosis: {exc}"]
        else:
            result = verify_prediction(impact, live_eval, live_document=doc)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    from . import flagsmith_io as fio

    ap = argparse.ArgumentParser(prog="python -m flaggate.verify", description=__doc__.split("CLI (")[0].strip())
    ap.add_argument("--plan", required=True)
    ap.add_argument("--before-env-doc", required=True, help="environment document captured BEFORE apply")
    ap.add_argument("--identities", required=True, help="identities CSV/JSON used for the preview")
    ap.add_argument("--live", action="store_true", required=True, help="query the live Flagsmith (env/.env creds)")
    args = ap.parse_args(argv)
    try:
        creds = fio.load_credentials()
        with open(args.plan) as fh:
            plan = Plan.model_validate_json(fh.read())
        with open(args.before_env_doc) as fh:
            before = json.load(fh)
        impact = compute_impact(before, plan, load_identities(args.identities), sample_source=args.identities)
        res = verify_live(impact, api_url=fio.api_url_from_env(), environment_key=creds["FS_ENV_KEY"],
                          server_key=creds.get("FS_SERVER_KEY"))
    except KeyError as exc:
        print(f"missing credential {exc.args[0]}", file=sys.stderr)
        return 2
    except (OSError, ValueError, fio.FlagsmithHTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(res.render())
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
