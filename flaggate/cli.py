"""``fg``: FlagGate command line.

  fg keygen                         create the approver Ed25519 key (passphrase-protected; TTY only)
  fg gatekeeper init ...            seal the Flagsmith keys for the gatekeeper and pin the approver public key
  fg gatekeeper start               run the gatekeeper daemon (unix socket); the only holder of the Flagsmith key
  fg snapshot                       current Flagsmith state hash (and JSON with --json)
  fg plan "<prompt>"                keyless rule-based planner -> submitted plan + diff + impact
  fg submit plan.json               submit a plan written by a coding agent
  fg show <plan_id>                 review bundle
  fg approve <plan_id>              HUMAN ONLY: re-check, type hash prefix + passphrase, sign, apply
  fg apply <plan_id>                apply with a token saved by `fg approve --no-apply`
  fg rollback <plan_id>             run the pre-approved rollback
  fg promote F --metrics m.csv      A/B winner from metrics -> promote plan (same approval gate)
  fg audit verify | fg audit show   hash-chain check / recent records

FG_HOME (default ~/.flaggate) holds approver/, gatekeeper/ and approvals/. FG_SOCKET overrides the (human) socket
path; FG_AGENT_SOCKET the agent socket (no apply/rollback routes; used by fg-mcp).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from flaggate.approval import (
    ApprovalError,
    generate_keypair,
    load_private_key,
    make_payload,
    public_bytes,
    sign_token,
)
from flaggate.diff import diff_states, render_text
from flaggate.hooks import render_impact
from flaggate.metrics import format_elapsed
from flaggate.plan import Plan, plan_hash
from flaggate.rollback import rollback_plan
from flaggate.server import GatekeeperClient, GatekeeperError
from flaggate.state import PlanConflict, Snapshot, simulate, state_hash

ROLLBACK_TTL_MIN = 24 * 60


def home() -> Path:
    return Path(os.environ.get("FG_HOME", Path.home() / ".flaggate")).expanduser()


def sock_path() -> Path:
    return Path(os.environ.get("FG_SOCKET", home() / "gatekeeper" / "gk.sock"))


def agent_sock_path() -> Path:
    return Path(os.environ.get("FG_AGENT_SOCKET", home() / "gatekeeper" / "agent.sock"))


def _client() -> GatekeeperClient:
    p = sock_path()
    if not p.exists():
        sys.exit(f"gatekeeper socket {p} not found: run `fg gatekeeper start` in another terminal")
    return GatekeeperClient(p)


def _isatty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _require_tty(what: str) -> None:
    if not _isatty():
        print(f"refusing: `{what}` needs an interactive terminal (stdin and stdout must be a TTY). "
              "Approvals cannot be piped or scripted.", file=sys.stderr)
        sys.exit(2)


def _new_passphrase(prompt: str, fd: int | None = None) -> str:
    if fd is not None:
        return os.fdopen(fd).readline().rstrip("\n")
    a = getpass.getpass(f"{prompt}: ")
    if getpass.getpass(f"{prompt} (again): ") != a:
        sys.exit("passphrases differ")
    return a


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")


def _print_review(b: dict[str, Any]) -> None:
    print(f"plan      {b['plan_id']}")
    print(f"hash      {b['plan_hash']}")
    if b.get("rollback_plan_hash"):
        print(f"rollback  {b['rollback_plan_hash']}  (pre-approved together with apply)")
    print("\nDIFF (plan vs live Flagsmith state)")
    print(b["diff_text"])
    print("\nIMPACT (Flagsmith flag engine on the environment document)")
    print(render_impact(b.get("impact")))
    for n in b.get("rollback_notes") or []:
        print(f"  note: {n}")
    print(f"\nmanual steps replaced: {b.get('manual_steps_replaced')}   approvable: {b.get('approvable')}"
          f"   status: {b.get('status')}")


def _fail(e: GatekeeperError) -> None:
    body = e.body
    if body.get("refused"):
        print(f"REFUSED ({body['reason']}): {body['detail']}", file=sys.stderr)
        sys.exit(3)
    print(f"error: {body}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- commands
def cmd_keygen(a: argparse.Namespace) -> None:
    _require_tty("fg keygen")
    d = home() / "approver"
    priv, pub = d / "approver.key.enc", d / "approver.pub"
    if priv.exists():
        sys.exit(f"{priv} already exists")
    fp = generate_keypair(priv, pub, _new_passphrase("New approver passphrase (min 8 chars)"))
    (d / "approver.json").write_text(json.dumps({"name": a.name}))
    print(f"approver key {fp} for {a.name!r}\n  private (encrypted): {priv}\n  public: {pub}")


def _read_env_file(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def cmd_gk_init(a: argparse.Namespace) -> None:
    from flaggate.secretbox import seal

    gdir = home() / "gatekeeper"
    gdir.mkdir(parents=True, exist_ok=True)
    os.chmod(gdir, 0o700)
    src = _read_env_file(a.from_env_file) if a.from_env_file else {}
    secrets = {
        "admin_api_key": src.get("FS_ADMIN_API_KEY") or getpass.getpass("Flagsmith organisation Admin API key: "),
        "env_key": src.get("FS_ENV_KEY") or getpass.getpass("Environment (client-side) key: "),
        "server_key": src.get("FS_SERVER_KEY") or getpass.getpass("Environment server-side key (ser.…): "),
    }
    pub = Path(a.approver_pub) if a.approver_pub else home() / "approver" / "approver.pub"
    if not pub.exists():
        sys.exit(f"approver public key {pub} not found: run `fg keygen` first (or pass --approver-pub)")
    (gdir / "approver.pub").write_text(pub.read_text())
    cfg = {"api_url": a.api_url.rstrip("/"), "organisation_id": a.organisation_id, "project_id": a.project_id,
           "environment": a.environment, "identities_csv": a.identities}
    (gdir / "config.json").write_text(json.dumps(cfg, indent=2))
    seal(gdir / "secrets.enc", secrets, _new_passphrase("New gatekeeper passphrase", a.passphrase_fd))
    print(f"gatekeeper initialised in {gdir} (secrets sealed; approver key pinned)")
    if a.from_env_file:
        print(f"  you can now delete or lock down {a.from_env_file}: the gatekeeper no longer needs it")


def build_gatekeeper(gdir: Path, passphrase: str) -> Any:
    from flaggate.approval import load_public_key
    from flaggate.client import FlagsmithClient
    from flaggate.gatekeeper import Gatekeeper, GatekeeperConfig
    from flaggate.hooks import FlagsmithImpact, identities_source
    from flaggate.secretbox import unseal

    cfg = json.loads((gdir / "config.json").read_text())
    sec = unseal(gdir / "secrets.enc", passphrase)
    gc = GatekeeperConfig(api_url=cfg["api_url"], organisation_id=cfg["organisation_id"], project_id=cfg["project_id"],
                          environment=cfg["environment"], env_key=sec["env_key"], server_key=sec["server_key"])
    client = FlagsmithClient(gc.api_url, sec["admin_api_key"], secrets=(sec["env_key"], sec["server_key"]))
    ident_fn, source = identities_source(gc.api_url, sec["admin_api_key"], sec["env_key"], cfg.get("identities_csv"))
    impact = FlagsmithImpact(api_url=gc.api_url, env_key=gc.env_key, server_key=gc.server_key,
                             identities=ident_fn, sample_source=source)
    return Gatekeeper(state_dir=gdir / "state", config=gc, approver_pub=load_public_key(gdir / "approver.pub"),
                      client=client, impact=impact)


def cmd_gk_start(a: argparse.Namespace) -> None:
    from flaggate.gatekeeper import harden_process
    from flaggate.server import serve

    gdir = home() / "gatekeeper"
    if a.passphrase_fd is not None:
        passphrase = os.fdopen(a.passphrase_fd).readline().rstrip("\n")
    else:
        _require_tty("fg gatekeeper start (without --passphrase-fd)")
        passphrase = getpass.getpass("Gatekeeper passphrase: ")
    hardened = harden_process()
    try:
        gk = build_gatekeeper(gdir, passphrase)
    except ValueError as e:
        sys.exit(str(e))
    del passphrase
    for k in [k for k in os.environ if k.startswith(("FS_", "FLAGSMITH_API_KEY"))]:
        os.environ.pop(k, None)
    if not gk.client.ping():
        sys.exit("cannot reach Flagsmith with the sealed credentials")
    import signal
    import threading

    lock = threading.Lock()  # one decision at a time across both sockets
    srv = serve(gk, sock_path(), role="human", lock=lock)
    agent_srv = serve(gk, agent_sock_path(), role="agent", lock=lock)
    threading.Thread(target=agent_srv.serve_forever, daemon=True).start()
    def _stop(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    (gdir / "gatekeeper.pid").write_text(str(os.getpid()))
    print(f"gatekeeper pid={os.getpid()} socket={sock_path()} agent-socket={agent_sock_path()} api={gk.cfg.api_url} "
          f"project={gk.cfg.project_id} env={gk.cfg.environment} approver={gk.approver_fp} non-dumpable={hardened}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        agent_srv.shutdown()
        for s_, p_ in ((srv, sock_path()), (agent_srv, agent_sock_path())):
            s_.server_close()
            p_.unlink(missing_ok=True)
        (gdir / "gatekeeper.pid").unlink(missing_ok=True)


def cmd_snapshot(a: argparse.Namespace) -> None:
    r = _client().snapshot()
    print(json.dumps(r["snapshot"], indent=2) if a.json else r["state_hash"])


def cmd_plan(a: argparse.Namespace) -> None:
    try:
        b = _client().plan_from_prompt(a.prompt)
    except GatekeeperError as e:
        _fail(e)
    print(json.dumps(b, indent=2, default=str) if a.json else "")
    if not a.json:
        _print_review(b)
        print(f"\nnext: a human runs `fg approve {b['plan_id']}`")


def cmd_submit(a: argparse.Namespace) -> None:
    plan = json.loads(Path(a.file).read_text())
    try:
        b = _client().submit(plan, actor=a.actor)
    except GatekeeperError as e:
        _fail(e)
    _print_review(b)


def cmd_show(a: argparse.Namespace) -> None:
    try:
        b = _client().review(a.plan_id)
    except GatekeeperError as e:
        _fail(e)
    if a.json:
        b.pop("snapshot", None)
        print(json.dumps(b, indent=2, default=str))
    else:
        _print_review(b)


def _tok_path(plan_id: str, action: str) -> Path:
    return home() / "approvals" / f"{plan_id}.{action}.tok"


def cmd_approve(a: argparse.Namespace) -> None:
    _require_tty("fg approve")  # checked first, before any network or key access
    c = _client()
    try:
        b = c.review(a.plan_id)
    except GatekeeperError as e:
        _fail(e)
    plan = Plan.model_validate(b["plan"])
    local_hash = plan_hash(plan)
    if local_hash != b["plan_hash"]:
        sys.exit(f"ABORT: gatekeeper says hash {b['plan_hash'][:12]} but the plan content hashes to {local_hash[:12]}")
    snap = Snapshot.model_validate(b["snapshot"])
    if state_hash(snap) != b["live_state_hash"]:
        sys.exit("ABORT: snapshot from gatekeeper does not match its state hash")
    try:
        changes = diff_states(snap, simulate(snap, plan.ops))  # recomputed here, not taken from the agent
    except PlanConflict as e:
        sys.exit(f"ABORT: plan no longer applies to the live state: {e}")
    print(f"PLAN {plan.plan_id}   by {plan.author}\nprompt: {plan.prompt}\n")
    print(f"hash  {local_hash}   (recomputed locally)")
    print(f"base  {plan.base_state_hash}   live {b['live_state_hash']}"
          + ("   OK" if plan.base_state_hash == b["live_state_hash"] else "   DRIFT: apply will be refused"))
    print("\nDIFF (recomputed locally from the live snapshot)")
    print(render_text(changes, color=True))
    print("\nIMPACT (computed by the gatekeeper with the Flagsmith flag engine)")
    print(render_impact(plan.impact))
    for n in b.get("rollback_notes") or []:
        print(f"  note: {n}")
    print("\nROLLBACK (pre-approved with this approval, single use, 24h)")
    print("\n".join(f"  {i + 1}. {op.op} {op.model_dump(exclude={'op'}, exclude_none=True)}"
                    for i, op in enumerate(plan.rollback.ops)) or "  (none)")
    if not (plan.impact or {}).get("approvable"):
        sys.exit("\nNOT APPROVABLE: the impact preview breaks a guardrail or could not be evaluated")
    typed = input(f"\nType the first 4 characters of the plan hash to approve ({local_hash[:2]}..): ").strip()
    if typed != local_hash[:4]:
        sys.exit("hash prefix does not match: not approved")
    adir = home() / "approver"
    name = json.loads((adir / "approver.json").read_text()).get("name", "approver") if (adir / "approver.json").exists() else "approver"
    try:
        key = load_private_key(adir / "approver.key.enc", getpass.getpass("Approver passphrase: "))
    except ApprovalError as e:
        sys.exit(str(e))
    apply_tok = sign_token(key, make_payload(plan, "apply", name, plan.guardrails.token_ttl_min))
    _write_secret(_tok_path(plan.plan_id, "apply"), apply_tok)
    if plan.rollback.ops:
        rb = rollback_plan(plan)
        _write_secret(_tok_path(plan.plan_id, "rollback"), sign_token(key, make_payload(rb, "rollback", name, ROLLBACK_TTL_MIN)))
    del key
    print(f"approved by {name}: apply token + pre-approved rollback token saved under {home() / 'approvals'}")
    if a.no_apply:
        print(f"apply later with `fg apply {plan.plan_id}` (token expires in {plan.guardrails.token_ttl_min} min)")
        return
    _do_apply(c, plan.plan_id, apply_tok)


def _do_apply(c: GatekeeperClient, plan_id: str, token: str) -> None:
    try:
        r = c.apply(plan_id, token)
    except GatekeeperError as e:
        _fail(e)
    if r.get("ok"):
        v = r.get("verify") or {}
        print(f"APPLIED {plan_id}: {r['flagsmith_calls']} Flagsmith calls, two-phase={r.get('two_phase')}, "
              f"prediction matched {v.get('prediction_match')} (audit #{r['audit_seq']})")
        print(f"manual steps replaced: {r.get('manual_steps_replaced')}   "
              f"elapsed prompt→applied: {format_elapsed(r.get('elapsed_s'))}")
    else:
        print(f"NOT APPLIED ({r.get('status')}): {r.get('detail') or r.get('error')}; "
              f"compensated={r.get('compensated')} (audit #{r['audit_seq']})")
        sys.exit(4)


def cmd_apply(a: argparse.Namespace) -> None:
    p = Path(a.token_file) if a.token_file else _tok_path(a.plan_id, "apply")
    token = p.read_text().strip() if p.exists() else None
    c = _client()
    _do_apply(c, a.plan_id, token)


def cmd_rollback(a: argparse.Namespace) -> None:
    p = _tok_path(a.plan_id, "rollback")
    token = p.read_text().strip() if p.exists() else None
    try:
        r = _client().rollback(a.plan_id, token)
    except GatekeeperError as e:
        _fail(e)
    print(f"ROLLED BACK {a.plan_id}: {r['flagsmith_calls']} Flagsmith calls, "
          f"restored base state={r.get('restored_base_state')} (audit #{r['audit_seq']})")


def cmd_promote(a: argparse.Namespace) -> None:
    from flaggate.promote import PromoteError, analysis_dict, analyze, promote_plan, read_metrics

    c = _client()
    snap = Snapshot.model_validate(c.snapshot()["snapshot"])
    try:
        an = analyze(snap, a.feature, read_metrics(Path(a.metrics), a.feature), a.min_exposures, a.alpha)
        print("METRICS  " + an.summary())
        for n in an.reasons:
            print(f"  note: {n}")
        if a.json:
            print(json.dumps(analysis_dict(an), indent=2))
        plan = promote_plan(snap, an, Path(a.metrics), force=a.force)
    except PromoteError as e:
        print(f"not promoting: {e}", file=sys.stderr)
        sys.exit(4)
    try:
        b = c.submit(json.loads(plan.model_dump_json(by_alias=True)), actor="fg:promote")
    except GatekeeperError as e:
        _fail(e)
    print()
    _print_review(b)
    print(f"\nnext: a human runs `fg approve {b['plan_id']}`")


def cmd_audit(a: argparse.Namespace) -> None:
    if a.what == "verify":
        r = _client().audit_verify() if not a.file else _local_verify(Path(a.file))
        print(("OK   " if r["ok"] else "FAIL ") + r["message"]
              + (f"; live state matches last change: {r['live_matches_last_change']}" if "live_matches_last_change" in r else ""))
        sys.exit(0 if r["ok"] else 5)
    from flaggate.audit import AuditLog
    path = Path(a.file) if a.file else home() / "gatekeeper" / "state" / "audit.jsonl"
    for r in AuditLog(path).records()[-a.n:]:
        extra = r.get("reason") or r.get("prediction_match") or r.get("error") or ""
        print(f"#{r['seq']:<3} {r['ts']}  {r['event']:<15} {r.get('plan_id', ''):<28} {extra}")


def _local_verify(path: Path) -> dict[str, Any]:
    from flaggate.audit import AuditLog
    ok, n, msg = AuditLog(path).verify()
    return {"ok": ok, "records": n, "message": msg}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fg", description="FlagGate: human-gated Flagsmith changes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("keygen"); s.add_argument("--name", default=getpass.getuser()); s.set_defaults(fn=cmd_keygen)
    g = sub.add_parser("gatekeeper").add_subparsers(dest="gcmd", required=True)
    s = g.add_parser("init")
    s.add_argument("--api-url", default=os.environ.get("FLAGSMITH_API_URL", "http://127.0.0.1:8600"),
                   help="Flagsmith API (default: $FLAGSMITH_API_URL, else http://127.0.0.1:8600)")
    s.add_argument("--organisation-id", type=int, required=True)
    s.add_argument("--project-id", type=int, required=True)
    s.add_argument("--environment", required=True, help="environment name, e.g. Development")
    s.add_argument("--from-env-file", help="read FS_ADMIN_API_KEY / FS_ENV_KEY / FS_SERVER_KEY from this file")
    s.add_argument("--approver-pub")
    s.add_argument("--identities", help="CSV/JSON identities for the impact preview (default: Flagsmith identities API)")
    s.add_argument("--passphrase-fd", type=int)
    s.set_defaults(fn=cmd_gk_init)
    s = g.add_parser("start"); s.add_argument("--passphrase-fd", type=int); s.set_defaults(fn=cmd_gk_start)
    s = sub.add_parser("snapshot"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_snapshot)
    s = sub.add_parser("plan"); s.add_argument("prompt"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("submit"); s.add_argument("file"); s.add_argument("--actor", default="agent:cli"); s.set_defaults(fn=cmd_submit)
    s = sub.add_parser("show"); s.add_argument("plan_id"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("approve"); s.add_argument("plan_id"); s.add_argument("--no-apply", action="store_true"); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("apply"); s.add_argument("plan_id"); s.add_argument("--token-file"); s.set_defaults(fn=cmd_apply)
    s = sub.add_parser("rollback"); s.add_argument("plan_id"); s.set_defaults(fn=cmd_rollback)
    s = sub.add_parser("promote", help="pick an A/B winner from metrics.csv and submit a promote plan")
    s.add_argument("feature"); s.add_argument("--metrics", required=True)
    s.add_argument("--min-exposures", type=int, default=100); s.add_argument("--alpha", type=float, default=0.05)
    s.add_argument("--force", action="store_true", help="promote even if not significant (recorded in the plan)")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_promote)
    s = sub.add_parser("audit"); s.add_argument("what", choices=["verify", "show"]); s.add_argument("-n", type=int, default=30)
    s.add_argument("--file", help="audit.jsonl to read directly (offline)"); s.set_defaults(fn=cmd_audit)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
