"""Test-only helpers: a throw-away Flagsmith project/environment on the local self-host and a tiny applier for the
three op types the integration test uses. (dev2 owns the real executor; this is not it.)

Nothing here prints credentials. Everything created lives in its own project, which is deleted afterwards, so the
demo seed (project 'checkout-app') is never touched.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any

from flaggate import Plan
from flaggate.flagsmith_io import FlagsmithHTTPError


class Admin:
    def __init__(self, api_url: str, admin_key: str, scheme: str = "Api-Key"):
        self.api, self.key, self.scheme = api_url.rstrip("/"), admin_key, scheme

    def call(self, method: str, path: str, body: Any = None) -> Any:
        req = urllib.request.Request(
            self.api + path, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": f"{self.scheme} {self.key}"} if self.key else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise FlagsmithHTTPError(f"{method} {path} -> HTTP {exc.code} {exc.read()[:200]!r}") from None

    def results(self, path: str) -> list[dict]:
        d = self.call("GET", path)
        return d["results"] if isinstance(d, dict) and "results" in d else d


class LiveProject:
    """Own *organisation* (the free plan allows one project per org, and the demo seed owns the first org's
    project) with its own project + environment + server key + the demo features (new_checkout,
    checkout_button_color). Created through the local superuser login; torn down by deleting the org."""

    def __init__(self, api_url: str, email: str, password: str):
        self.api_url, self.email, self.password = api_url, email, password
        self.org: int | None = None
        self.user: Admin | None = None
        self.project_id: int | None = None
        self.env_key = self.server_key = ""
        self.features: dict[str, dict] = {}
        self.seg_ids: dict[str, int] = {}

    def create(self) -> "LiveProject":
        tok = Admin(self.api_url, "").call("POST", "/api/v1/auth/login/", {"email": self.email, "password": self.password})["key"]
        self.user = Admin(self.api_url, tok, scheme="Token")
        org = self.user.call("POST", "/api/v1/organisations/", {"name": f"fg-impact-it-{uuid.uuid4().hex[:8]}"})
        self.org = org["id"]
        mk = self.user.call("POST", f"/api/v1/organisations/{self.org}/master-api-keys/",
                            {"name": "fg-impact-it", "is_admin": True})["key"]
        self.admin = a = Admin(self.api_url, mk)
        proj = a.call("POST", "/api/v1/projects/", {"name": f"fg-impact-it-{uuid.uuid4().hex[:8]}",
                                                    "organisation": self.org})
        self.project_id = proj["id"]
        envs = a.results(f"/api/v1/environments/?project={self.project_id}")
        env = envs[0] if envs else a.call("POST", "/api/v1/environments/", {"name": "it", "project": self.project_id})
        self.env_id, self.env_key = env["id"], env["api_key"]
        self.server_key = a.call("POST", f"/api/v1/environments/{self.env_key}/api-keys/", {"name": "it"})["key"]
        pid = self.project_id
        self.features["new_checkout"] = a.call("POST", f"/api/v1/projects/{pid}/features/", {
            "name": "new_checkout", "type": "STANDARD", "default_enabled": False})
        self.features["checkout_button_color"] = a.call("POST", f"/api/v1/projects/{pid}/features/", {
            "name": "checkout_button_color", "type": "MULTIVARIATE", "default_enabled": True, "initial_value": "blue",
            "multivariate_options": [
                {"type": "unicode", "string_value": "green", "default_percentage_allocation": 33},
                {"type": "unicode", "string_value": "orange", "default_percentage_allocation": 33}]})
        return self

    def delete(self) -> None:
        """Remove everything this helper created: its own organisation (and with it the project)."""
        if self.org is not None and self.user is not None:
            self.user.call("DELETE", f"/api/v1/organisations/{self.org}/")
            self.org = None

    # --- minimal applier --------------------------------------------------------------------------
    def apply(self, plan: Plan, *, only: tuple[str, ...] | None = None, skip: tuple[str, ...] = ()) -> None:
        """Apply ops in order. ``only``/``skip`` filter by op name so a caller can apply in phases
        (e.g. segments first, to learn their server-assigned ids); @refs persist across phases."""
        a, pid = self.admin, self.project_id
        seg_ids = self.seg_ids

        def seg_id(ref: str) -> int:
            return seg_ids[ref[1:]] if ref.startswith("@") else int(ref)

        for op in plan.ops:
            if (only is not None and op.op not in only) or op.op in skip:
                continue
            if op.op == "create_segment":
                body = {"name": op.name, "project": pid, "rules": [_rule_body(r) for r in op.rules]}
                seg = a.call("POST", f"/api/v1/projects/{pid}/segments/", body)
                if op.ref:
                    seg_ids[op.ref] = seg["id"]
            elif op.op == "set_segment_override":
                f = self.features[op.feature]
                a.call("POST", f"/api/v1/environments/{self.env_key}/features/{f['id']}/create-segment-override/", {
                    "enabled": op.enabled, "feature_state_value": {"type": "unicode", "string_value": op.value or ""},
                    "feature_segment": {"segment": seg_id(op.segment), "priority": op.priority}})
            elif op.op == "set_mv_weights":
                f = self.features[op.feature]
                states = a.results(f"/api/v1/environments/{self.env_key}/featurestates/?feature={f['id']}")
                fs = next(s for s in states if s.get("feature_segment") is None and s.get("identity") is None)
                for m in fs["multivariate_feature_state_values"]:
                    m["percentage_allocation"] = op.weights[_mv_value(a, pid, f["id"], m)]
                a.call("PUT", f"/api/v1/environments/{self.env_key}/featurestates/{fs['id']}/", fs)   # PATCH is ignored for MV rows
            else:  # pragma: no cover
                raise NotImplementedError(op.op)


def _rule_body(rule) -> dict:
    """Plan rule -> Admin API rule. The API stores `property` as nullable-but-not-blank (v2.295.2), so the plan's
    empty property (PERCENTAGE_SPLIT) must be sent as null."""
    return {"type": rule.type, "rules": [_rule_body(r) for r in rule.rules],
            "conditions": [{"property": c.property or None, "operator": c.operator, "value": c.value}
                           for c in rule.conditions]}


def _mv_value(a: Admin, pid: int, fid: int, m: dict) -> str:
    opts = a.results(f"/api/v1/projects/{pid}/features/{fid}/mv-options/")
    return next(o["string_value"] for o in opts if o["id"] == m["multivariate_feature_option"])
