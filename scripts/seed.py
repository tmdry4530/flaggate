#!/usr/bin/env python3
"""Seed + smoke-test a self-hosted Flagsmith through the Admin (Management) API.

Creates (idempotently): organisation, project, environment, an org Admin API key,
a server-side env key, a boolean flag, a remote-config flag, a multivariate flag
(A/B/C split), a segment (plan == "pro") and a segment override.
Then reads flags back with the OFFICIAL Flagsmith Python SDK (remote + local evaluation).

Secrets (admin password/API keys/env keys) are read from / appended to
$FLAGSMITH_RUNTIME/local.env (chmod 600) and are never printed (only masked).
Non-secret ids go to $FLAGSMITH_RUNTIME/seed-state.json.
Run with the Flagsmith API venv python (it already ships the `flagsmith` SDK):
    $FLAGSMITH_RUNTIME/venv/bin/python scripts/seed.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

RUNTIME = Path(os.environ.get("FLAGSMITH_RUNTIME", "../flagsmith-runtime"))
API = os.environ.get("FLAGSMITH_API_URL", f"http://127.0.0.1:{os.environ.get('API_PORT', '8600')}").rstrip("/")
ENV_FILE = RUNTIME / "local.env"
STATE_FILE = RUNTIME / "seed-state.json"

ORG, PROJECT, ENVIRONMENT = "Toggle Wars Demo", "checkout-app", "Development"


def load_env() -> dict[str, str]:
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def save_secret(k: str, v: str) -> None:
    with ENV_FILE.open("a") as f:
        f.write(f"{k}={v}\n")
    os.chmod(ENV_FILE, 0o600)


def mask(s: str) -> str:
    return (s[:4] + "…" + f"({len(s)} chars)") if s else "<none>"


SECRETS: set[str] = set()


def redact(s: str) -> str:
    for sec in SECRETS:
        s = s.replace(sec, mask(sec))
    return s


class Api:
    def __init__(self, auth: str):
        self.auth = auth

    def call(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        req = urllib.request.Request(
            f"{API}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": self.auth} if self.auth else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                print(redact(f"  {method:6} {path} -> {r.status}"))
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            print(redact(f"  {method:6} {path} -> {e.code} {e.read()[:300]!r}"), file=sys.stderr)
            raise

    def results(self, path):
        d = self.call("GET", path)
        return d["results"] if isinstance(d, dict) and "results" in d else d


def find(items, **kv):
    return next((i for i in items if all(i.get(k) == v for k, v in kv.items())), None)


def main() -> int:
    env = load_env()
    SECRETS.update(v for k, v in env.items() if k.startswith("FS_") and k != "FS_ADMIN_EMAIL")
    print("[1] login (Token auth) as local superuser")
    tok = Api(None).call("POST", "/api/v1/auth/login/",
                         {"email": env["FS_ADMIN_EMAIL"], "password": env["FS_ADMIN_PASSWORD"]})["key"]
    user = Api(f"Token {tok}")

    print("[2] organisation")
    org = find(user.results("/api/v1/organisations/"), name=ORG) or user.call("POST", "/api/v1/organisations/", {"name": ORG})

    print("[3] organisation Admin API key (Api-Key auth, used by CLI / MCP / our agent)")
    if "FS_ADMIN_API_KEY" not in env:
        k = user.call("POST", f"/api/v1/organisations/{org['id']}/master-api-keys/",
                      {"name": "toggle-wars-agent", "is_admin": True})["key"]
        save_secret("FS_ADMIN_API_KEY", k)
        env["FS_ADMIN_API_KEY"] = k
    api = Api(f"Api-Key {env['FS_ADMIN_API_KEY']}")
    print("    admin api key:", mask(env["FS_ADMIN_API_KEY"]))

    print("[4] project + environment (via Admin API key)")
    proj = find(api.results(f"/api/v1/projects/?organisation={org['id']}"), name=PROJECT) \
        or api.call("POST", "/api/v1/projects/", {"name": PROJECT, "organisation": org["id"]})
    envo = find(api.results(f"/api/v1/environments/?project={proj['id']}"), name=ENVIRONMENT) \
        or api.call("POST", "/api/v1/environments/", {"name": ENVIRONMENT, "project": proj["id"]})
    ekey = envo["api_key"]
    SECRETS.add(ekey)
    if env.get("FS_ENV_KEY") != ekey:
        save_secret("FS_ENV_KEY", ekey)
    if "FS_SERVER_KEY" not in env:
        sk = api.call("POST", f"/api/v1/environments/{ekey}/api-keys/", {"name": "sdk-local-eval"})["key"]
        save_secret("FS_SERVER_KEY", sk)
        env["FS_SERVER_KEY"] = sk
    print("    env key:", mask(ekey), " server key:", mask(env["FS_SERVER_KEY"]))

    print("[5] flags")
    feats = api.results(f"/api/v1/projects/{proj['id']}/features/")
    def ensure_feature(body):
        return find(feats, name=body["name"]) or api.call("POST", f"/api/v1/projects/{proj['id']}/features/", body)
    f_bool = ensure_feature({"name": "new_checkout", "type": "STANDARD", "default_enabled": False,
                             "description": "new checkout flow (bool)"})
    f_cfg = ensure_feature({"name": "checkout_banner", "type": "STANDARD", "default_enabled": True,
                            "initial_value": "Free shipping over $50", "description": "remote config"})
    f_mv = ensure_feature({"name": "checkout_button_color", "type": "MULTIVARIATE", "default_enabled": True,
                           "initial_value": "blue", "description": "A/B/C experiment",
                           "multivariate_options": [
                               {"type": "unicode", "string_value": "green", "default_percentage_allocation": 33},
                               {"type": "unicode", "string_value": "orange", "default_percentage_allocation": 33}]})
    mv = api.results(f"/api/v1/projects/{proj['id']}/features/{f_mv['id']}/mv-options/")
    print("    mv options:", [(o["string_value"], o["default_percentage_allocation"]) for o in mv])

    print("[6] segment plan == pro")
    segs = api.results(f"/api/v1/projects/{proj['id']}/segments/")
    seg = find(segs, name="pro_users") or api.call("POST", f"/api/v1/projects/{proj['id']}/segments/", {
        "name": "pro_users", "project": proj["id"],
        "rules": [{"type": "ALL", "rules": [{"type": "ANY", "rules": [], "conditions": [
            {"property": "plan", "operator": "EQUAL", "value": "pro"}]}]}]})

    print("[7] segment override: new_checkout ON for pro_users")
    fsegs = api.results(f"/api/v1/features/feature-segments/?environment={envo['id']}&feature={f_bool['id']}")
    if not find(fsegs, segment=seg["id"]):
        api.call("POST", f"/api/v1/environments/{ekey}/features/{f_bool['id']}/create-segment-override/", {
            "enabled": True, "feature_state_value": {"type": "unicode", "string_value": ""},
            "feature_segment": {"segment": seg["id"]}})

    print("[8] environment feature state update: enable checkout_banner text change")
    fs = api.results(f"/api/v1/environments/{ekey}/featurestates/?feature={f_cfg['id']}")
    env_fs = next(x for x in fs if x.get("feature_segment") is None and x.get("identity") is None)
    api.call("PATCH", f"/api/v1/environments/{ekey}/featurestates/{env_fs['id']}/",
             {"enabled": True, "feature_state_value": "Free shipping over $40"})

    STATE_FILE.write_text(json.dumps({"api": API, "organisation_id": org["id"], "project_id": proj["id"],
        "environment_id": envo["id"], "features": {f["name"]: f["id"] for f in (f_bool, f_cfg, f_mv)},
        "segment_id": seg["id"]}, indent=2))

    print("[9] official Flagsmith Python SDK read-back")
    from flagsmith import Flagsmith
    import flagsmith as fsmod
    print("    flagsmith SDK version:", getattr(fsmod, "__version__", "?"))
    sdk = Flagsmith(environment_key=ekey, api_url=f"{API}/api/v1/")
    flags = sdk.get_environment_flags()
    print("    env flags:", {n: (flags.is_feature_enabled(n), flags.get_feature_value(n))
                             for n in ("new_checkout", "checkout_banner", "checkout_button_color")})
    for ident, traits in (("alice", {"plan": "pro"}), ("bob", {"plan": "free"})):
        f = sdk.get_identity_flags(ident, traits=traits)
        print(f"    identity {ident} {traits}: new_checkout={f.is_feature_enabled('new_checkout')} "
              f"button={f.get_feature_value('checkout_button_color')}")
    dist = {}
    for i in range(60):
        v = sdk.get_identity_flags(f"user-{i}").get_feature_value("checkout_button_color")
        dist[v] = dist.get(v, 0) + 1
    print("    multivariate split over 60 identities:", dist)

    local = Flagsmith(environment_key=env["FS_SERVER_KEY"], api_url=f"{API}/api/v1/",
                      enable_local_evaluation=True, environment_refresh_interval_seconds=60)
    lf = local.get_identity_flags("alice", traits={"plan": "pro"})
    print("    LOCAL evaluation (server key, env document): alice new_checkout=",
          lf.is_feature_enabled("new_checkout"))
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
