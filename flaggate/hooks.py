"""Gatekeeper <-> impact engine glue (the engine itself is dev3's flaggate.impact / flaggate.verify).

The gatekeeper needs three things from an impact engine, matching the two-phase apply in docs/IMPACT.md:

* ``preview(plan)``      -> result with ``.report`` (JSON dict, stored as ``plan.impact``) and ``.approvable``
* ``repredict(result)``  -> the same preview re-run with the real, server-assigned segment ids
                            (call after phase 1 has created the plan's segments)
* ``verify(result)``     -> dict with at least ``ok`` and ``prediction_match`` (post-apply live SDK check)

``FlagsmithImpact`` implements this on top of compute_impact / repredict_with_live_ids / verify_live.
Unit tests inject their own fake with the same three methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from flaggate.plan import Plan


class ImpactLike(Protocol):
    report: dict[str, Any]

    @property
    def approvable(self) -> bool: ...


class ImpactEngine(Protocol):
    def preview(self, plan: Plan) -> ImpactLike: ...

    def repredict(self, result: ImpactLike) -> ImpactLike: ...

    def verify(self, result: ImpactLike) -> dict[str, Any]: ...


IdentitiesFn = Callable[[], Sequence[Any]]


def identities_source(api_url: str, admin_key: str, env_key: str, csv_path: str | None = None,
                      limit: int = 200) -> tuple[IdentitiesFn, str]:
    """Identities for the preview: a CSV/JSON file if given, else the environment's identities via the Admin API."""
    if csv_path:
        from flaggate.identities import load_identities
        return (lambda: load_identities(Path(csv_path))), f"file {Path(csv_path).name}"
    from flaggate.flagsmith_io import fetch_identities
    return (lambda: fetch_identities(api_url, admin_key, env_key, limit=limit)), f"flagsmith identities API (≤{limit})"


class FlagsmithImpact:
    def __init__(self, *, api_url: str, env_key: str, server_key: str, identities: IdentitiesFn,
                 sample_source: str = "flagsmith identities"):
        self.api_url, self._env_key, self._server_key = api_url, env_key, server_key
        self._identities, self.sample_source = identities, sample_source

    def _doc(self) -> dict[str, Any]:
        from flaggate.flagsmith_io import fetch_environment_document
        return fetch_environment_document(self.api_url, self._server_key)

    def preview(self, plan: Plan) -> Any:
        from flaggate.impact import compute_impact
        return compute_impact(self._doc(), plan, list(self._identities()), sample_source=self.sample_source)

    def repredict(self, result: Any) -> Any:
        from flaggate.verify import repredict_with_live_ids
        return repredict_with_live_ids(result, self._doc())

    def verify(self, result: Any) -> dict[str, Any]:
        from flaggate.verify import verify_live
        out = verify_live(result, api_url=self.api_url, environment_key=self._env_key,
                          server_key=self._server_key, transient=True)
        return {**out.to_dict(), "ok": out.ok, "prediction_match": out.prediction_match, "summary": out.summary()}


def render_impact(report: dict[str, Any] | None) -> str:
    if not report:
        return "  (no impact report)"
    try:
        from flaggate.impact import render_text
        return render_text(report)
    except Exception:  # noqa: BLE001 - fake/partial reports in tests
        return "  " + ", ".join(f"{k}={v}" for k, v in report.items() if not isinstance(v, (dict, list)))
