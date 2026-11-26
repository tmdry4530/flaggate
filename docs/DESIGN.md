# FlagGate — an AI change agent for Flagsmith that cannot ship without a human

Toggle Wars (Flagsmith × WeMakeDevs), Track 2 "AI Agents and Feature Flags". Deadline 2026-11-29 23:30 KST.
Status: phase-1 design. Self-host spike results: [SELFHOST.md](SELFHOST.md).

## 1. Problem

Rolling out a feature safely in Flagsmith is 10–20 manual steps: create the flag, create a segment,
add a segment override, set percentage splits, set multivariate weights, check who is affected, write
down how to undo it, then later promote the winner. AI coding agents (Codex CLI, Claude Code) can now do
these steps through the official Flagsmith MCP server or CLI, but:

- the MCP/CLI credential is an **Admin API key**; whoever holds it writes to production directly;
- on self-hosted OSS Flagsmith, **native change requests are unavailable** (`minimum_change_request_approvals`
  is read-only on the free plan) and the **audit log API shows nothing** (`audit_log_visibility_days = 0`;
  with an Api-Key it even returns 500). Verified locally, see SELFHOST.md.

So today it's either "agent with god-mode key" or "human does everything by hand".

## 2. Idea

**FlagGate**: you type an intent ("roll out new checkout to 10% of pro users, A/B test the button
green vs orange, roll back if anything breaks"). The agent turns it into a **typed Flagsmith change plan**,
FlagGate shows a **diff + impact preview**, a **human approves** in a human-only prompt, and only then is the
plan applied to Flagsmith. Rollback and winner rollout are separate plans behind the same gate.
Every step leaves a hash-chained audit record.

### Differentiator (one)

> **Approve outcomes, not JSON: before you approve, FlagGate replays the plan against your real
> identities with Flagsmith's own flag engine and shows exactly who flips (e.g. "23 of 200 identities
> → `new_checkout` OFF→ON, all in `pro_users`, max exposure 11.5% ≤ guardrail 20%"), then re-checks the
> live SDK result after apply ("prediction matched 200/200").**

It's built on the Flagsmith environment document (`/api/v1/environment-document/`) plus
`flagsmith-flag-engine` (the engine the SDKs use for local evaluation) and the identities API. Without
Flagsmith it doesn't exist: no state, no engine, no apply target. That also covers the rule
"if it runs the same without Flagsmith, it scores nothing".

## 3. Architecture

```
 ┌──────────────────────────┐   stdio MCP    ┌──────────────────────────────┐
 │ User's coding agent       │──────────────▶│ fg-mcp  (no Flagsmith key)    │
 │ Codex CLI / Claude Code   │◀──────────────│ tools: snapshot, read_*,      │
 │ (the "brain", user's own  │               │  submit_plan, plan_from_prompt│
 │  login, no API key in us) │               │  plan_status, wait_for_result │
 └──────────────────────────┘               └──────────────┬───────────────┘
                                                           │ HTTP on a unix socket (0600)
       human terminal (TTY)                               ▼
 ┌──────────────────────────┐   signed token  ┌──────────────────────────────┐   Admin API (Api-Key)
 │ fg approve <plan_id>      │───────────────▶│ fg-gatekeeper (daemon)        │────────────────────▶ Flagsmith API
 │ re-renders diff+impact    │                │ holds the Flagsmith Admin key │   official MCP (stdio,
 │ itself, asks for passphrase│               │ in memory only; verifies      │   read-only tools proxied)
 │ → Ed25519 signature        │               │ tokens; applies; audit chain  │   Flagsmith CLI (evaluate,
 └──────────────────────────┘                └──────────────┬───────────────┘   env document) for checks
                                                           │
                                       state/audit.jsonl (append-only, hash chain)
                                       state/plans/<plan_id>.json
```

Components (Python 3.13, one package `flaggate`, CLI entry `fg`):

| Component | Role | Holds secrets? |
|---|---|---|
| `fg-gatekeeper` | Only process that can write to Flagsmith. Snapshot, validate, hash, diff, impact, apply, verify, audit. Started by the human (`fg gatekeeper start`, passphrase prompt). | Flagsmith Admin API key (decrypted into memory at start), approver **public** key |
| `fg-mcp` | stdio MCP server the agent launches. Thin client of the gatekeeper socket. Re-exports the **read-only** tools of the official Flagsmith MCP (gatekeeper filters on their `readOnlyHint=True` annotations) plus FlagGate tools. There's no apply tool that works without a token. | none |
| `fg plan "<prompt>"` | Keyless rule-based planner (fallback brain for the demo, no LLM). Same plan schema as the agent path. | none |
| `fg approve <plan_id>` | Human-only approval CLI. Needs a TTY, fetches the plan from the gatekeeper, **recomputes hash/diff/impact locally** (doesn't trust agent text), asks the human to type the 4-char hash prefix + passphrase, and signs. | approver private key (scrypt-encrypted file, passphrase never stored) |
| `fg rollback`, `fg promote` | Make new plans (inverse plan / winner rollout) that go through the same gate. | none |
| `fg audit verify` | Re-walks the hash chain and cross-checks live Flagsmith state. | none |
| `fg watch` | Live persona panel that reads flags with the official Python SDK (local evaluation) for the demo. | env server key (read-only SDK key) |

### Agent brain without API keys
- **Primary**: the user's own Codex CLI / Claude Code (whatever login they already have) gets `fg-mcp` in its
  MCP config. The agent reads state (`fg_snapshot`, proxied official read tools), drafts a plan JSON
  that follows the schema, and calls `fg_submit_plan`. FlagGate never calls an LLM and ships no key.
- **Fallback**: `fg plan` / MCP tool `fg_plan_from_prompt` is a deterministic grammar-based planner
  (regex + small vocabulary: "roll out X to N% of <segment>", "A/B test X: a vs b [p/q]", "only for
  <trait>=<value>", "promote winner", "roll back"). It's enough for the demo and for CI tests.

## 4. Approval that the agent cannot bypass

Threat: the agent (same Unix user, has a shell) tries to apply without a human, or to get a different plan
approved than the one the human saw.

1. **No key in the agent's reach.** The Flagsmith Admin API key is stored encrypted
   (`state/secrets.enc`, scrypt + AES-GCM under the approver passphrase). The gatekeeper decrypts it into memory
   at start, wipes it from its env, and calls `prctl(PR_SET_DUMPABLE, 0)` so the same UID can't ptrace it or
   read `/proc/<pid>/environ|mem`. `fg-mcp` and the agent never see it. (The seeding script's plaintext
   `local.env` is for the spike only. In the demo it's deleted after `fg init`.)
2. **Plan hash.** `plan_hash = sha256(canonical_json(plan − {plan_hash, impact, status}))` (sorted keys, no
   whitespace, UTF-8). It covers target (API URL, org, project, env fingerprint), `base_state_hash`, ops,
   rollback ops and guardrails.
3. **Approval token = Ed25519 signature by the human.** Payload:
   `{v:1, plan_id, plan_hash, base_state_hash, action: apply|rollback|promote, env_fp, approver, nonce, exp}`.
   The private key exists only encrypted (`~/.flaggate/approver.key.enc`), and the passphrase is typed into
   `fg approve`'s TTY prompt (`getpass` on `/dev/tty`; it refuses if stdin/stdout isn't a TTY). The gatekeeper
   stores only the **public** key, so reading every file on disk still doesn't let the agent mint a token.
4. **Apply path refuses unless** the signature is valid for the pinned public key, the recomputed hash of the
   stored plan equals `plan_hash`, the live Flagsmith state of the touched objects hashes to `base_state_hash`
   (drift → refuse, re-plan), `exp` hasn't passed (default 15 min), the nonce is unused (replay), and the
   impact guardrails hold (e.g. `max_exposure_pct`). Each refusal is audited with a reason.
5. **What the human sees comes from the gatekeeper, not the agent.** `fg approve` renders the diff/impact that
   it recomputed, so a chat summary that says "harmless" while the plan does something else doesn't matter.
6. **Pre-approved rollback.** When approving `apply`, the human signs a second token for the *inverse*
   plan (`action: rollback`, single use, valid 24 h). Rolling back in an incident takes one command, needs no
   new approval, and can't be used for anything else because it's bound to the inverse plan hash. Winner
   promotion is **never** pre-approved.
7. **Append-only audit log.** `state/audit.jsonl`: each record carries `prev_hash` and
   `record_hash = sha256(prev_hash ‖ canonical(record))`; `fg audit verify` detects edits or truncation. The
   gatekeeper opens it `O_APPEND`. Optionally it also mirrors each apply as a Flagsmith tag `fg:<plan_id>` on
   touched features, so the trail is visible inside Flagsmith too (the OSS audit API isn't).

Honest limits: a same-user agent with keylogging or root, or a human who approves without reading, is out of
scope. For stronger isolation, run the gatekeeper as another user or host. Nothing in the protocol changes.

## 5. Data shapes

### 5.1 Plan (`flaggate.plan/v1`)
Normative schema, import paths, canonical rules and a hashed example: [PLAN_SCHEMA.md](PLAN_SCHEMA.md).
`plan_hash()` returns a raw 64-char SHA-256 hex digest (no `sha256:` prefix). Multivariate `weights` maps must include the control variant and sum to 100.

```json
{
  "schema": "flaggate.plan/v1",
  "plan_id": "pl_20261128_7f3a",
  "created_at": "2026-11-28T14:02:11+09:00",
  "author": "agent:claude-code | planner:rules",
  "prompt": "Roll out new_checkout to 10% of pro users; A/B test checkout_button_color green vs orange 50/50",
  "target": {"api_url": "http://127.0.0.1:8600", "organisation_id": 1, "project_id": 1,
             "environment": "Development", "env_fp": "sha256:9c1e…"},
  "base_state_hash": "sha256:41d0…",
  "ops": [
    {"op": "create_segment", "ref": "seg_pro_10", "name": "pro_users_10pct",
     "rules": [{"type": "ALL", "rules": [{"type": "ALL", "conditions": [
        {"property": "plan", "operator": "EQUAL", "value": "pro"},
        {"property": "", "operator": "PERCENTAGE_SPLIT", "value": "10"}]}]}]},
    {"op": "set_segment_override", "feature": "new_checkout", "segment": "@seg_pro_10",
     "enabled": true, "value": null, "priority": 0},
    {"op": "set_mv_weights", "feature": "checkout_button_color",
     "weights": {"green": 50, "orange": 50}}
  ],
  "rollback": {"ops": [
    {"op": "delete_segment_override", "feature": "new_checkout", "segment": "@seg_pro_10"},
    {"op": "delete_segment", "segment": "@seg_pro_10"},
    {"op": "set_mv_weights", "feature": "checkout_button_color", "weights": {"green": 33, "orange": 33}}
  ]},
  "guardrails": {"max_exposure_pct": 20, "token_ttl_min": 15},
  "plan_hash": "sha256:…",
  "impact": {"…": "computed by gatekeeper, see 5.3"}
}
```
Op vocabulary (v1): `create_feature`, `create_mv_feature`, `add_variant`, `set_flag` (env default
enabled/value), `set_mv_weights`, `create_segment`, `update_segment_rules`, `set_segment_override`,
`delete_segment_override`, `delete_segment`, `archive_feature`. Rollback ops are generated by the gatekeeper from the
before-state snapshot. The agent may propose them, but the gatekeeper overwrites them.

### 5.2 Diff
```json
{"plan_id": "pl_…", "changes": [
  {"kind": "add",    "path": "segments/pro_users_10pct", "after": "plan == pro AND 10% split"},
  {"kind": "add",    "path": "features/new_checkout/overrides/pro_users_10pct", "after": {"enabled": true}},
  {"kind": "change", "path": "features/checkout_button_color/weights",
   "before": {"blue(control)": 34, "green": 33, "orange": 33}, "after": {"blue(control)": 0, "green": 50, "orange": 50}}
]}
```
Rendered in the terminal as a colored `+ / ~ / -` list next to the impact table.

### 5.3 Impact (the differentiator)
```json
{"engine": "flagsmith-flag-engine 11.1.2", "env_document_at": "2026-11-28T14:02:12+09:00",
 "sample": {"source": "flagsmith identities API (+ fixtures/personas.csv)", "n": 200},
 "flips": [
   {"feature": "new_checkout", "from": false, "to": true, "count": 23, "pct": 11.5,
    "by_segment": {"pro_users_10pct": 23}},
   {"feature": "checkout_button_color", "value_from": "blue", "value_to": "green|orange", "count": 68, "pct": 34.0}],
 "unchanged": 109, "max_exposure_pct": 11.5, "guardrails_ok": true}
```
How: GET environment document (server key) → deep copy → apply plan ops to the copy (pure function) →
evaluate every sampled identity before/after with `flag_engine` → aggregate. After apply: evaluate
the same identities through the live API (SDK / `flagsmith evaluate --identity`) and report
`prediction_match: 200/200`.
**Exposure** counts only on/off and value changes; reassigning an identity between multivariate variants is
reported separately as **variant shift** (optional cap `guardrails.max_variant_shift_pct`), and
`archive_feature` is shown as a no-op for evaluation (Flagsmith keeps serving archived flags).
**Two-phase apply (implemented):** new segments are created first, the impact is re-predicted with the real
segment ids (PERCENTAGE_SPLIT hashes on them); if that breaks a guardrail the gatekeeper deletes the new
segments, audits `halted`, and stops. Otherwise it applies the rest and compares the re-prediction with the
live post-apply verify.

### 5.4 Audit record
```json
{"seq": 17, "ts": "2026-11-28T14:03:40+09:00", "event": "applied",
 "plan_id": "pl_20261128_7f3a", "plan_hash": "sha256:…", "action": "apply",
 "actor": "human:chamdom (ed25519:5b2c…)", "via": "gatekeeper", "token_nonce": "n_93ad…",
 "flagsmith_calls": [{"method": "POST", "path": "/api/v1/projects/1/segments/", "status": 201},
                     {"method": "POST", "path": "/api/v1/environments/<env>/features/1/create-segment-override/", "status": 201}],
 "result_state_hash": "sha256:…", "prediction_match": "200/200",
 "prev_hash": "sha256:…", "record_hash": "sha256:…"}
```
Events: `plan_submitted`, `approval_requested`, `approved`, `refused{reason}`, `applied`, `apply_failed`
(with the auto-compensation result), `rolled_back`, `promoted`, `verify_ok|verify_mismatch`.

## 6. Flagsmith surfaces used (all exercised locally on v2.295.2)

Admin API (`Authorization: Api-Key <org admin key>`):

| Purpose | Endpoint |
|---|---|
| state snapshot | `GET /api/v1/organisations/`, `GET /api/v1/projects/?organisation=`, `GET /api/v1/environments/?project=` |
| features | `GET/POST /api/v1/projects/{p}/features/`, `GET/POST/PUT /api/v1/projects/{p}/features/{f}/mv-options/` |
| segments | `GET/POST/PUT/DELETE /api/v1/projects/{p}/segments/` (`PERCENTAGE_SPLIT`, `EQUAL`, … operators) |
| flag state + segment overrides (what `flaggate/executor.py` uses) | `GET/PATCH /api/__future__/environments/{env}/features/{f}/` (`environment_default` / `segment_overrides`, incl. MV `variants` weights; the same endpoint the official CLI v2 uses), `DELETE …/segment-overrides/{segment}/`; read-back of overrides `GET /api/v1/features/featurestates/?environment=&feature=` |
| v1 pitfalls found on v2.295.2 | PATCHing MV weights through the v1 env featurestate is silently ignored and `PATCH /api/v1/features/featurestates/{id}/` returns 500, so v1 is used only for features, mv-options, segments and archive (`PATCH …/features/{f}/ {is_archived}`); `PERCENTAGE_SPLIT` conditions must send `property: null` (`""` → 400, missing → 500) |
| impact | `GET /api/v1/environment-document/` (server key), `GET /api/v1/environments/{env}/identities/` (+ traits) |
| keys for SDK | `POST /api/v1/environments/{env}/api-keys/` |
| traceability | `POST /api/v1/projects/{p}/tags/` + feature `tags` (mirror `fg:<plan_id>`) |

Official **Flagsmith MCP** (stdio, inside the gatekeeper): read-only tools proxied to the agent
(`list_project_features`, `list_project_segments`, `get_feature_evaluation_data`, …).
Official **Flagsmith CLI** v2: `flagsmith evaluate --identity` and `flagsmith environment document` for
post-apply verification output in the demo (`TARGETING_MATCH; segment=…`, `SPLIT; weight=…`).
Official **Python SDK** 6.3.0: `fg watch` persona panel (local evaluation).

## 7. Measuring "saves real time"
Every run prints `manual steps replaced: N` (the Flagsmith calls plus UI pages a human would click) and
wall-clock from prompt to applied. Target for the demo intent: ~14 manual dashboard steps + a manual
"who is affected" check → 1 prompt + 1 approval, about 40 s.

## 8. Build plan to 11/29 23:30 KST

| Day | Deliverable | Done when |
|---|---|---|
| **Thu 11/26** (today) | Self-host spike, design, demo script | `selfhost-up.sh`/`seed.sh` reproducible; CLI + MCP verified; this doc |
| **Fri 11/27** | `flaggate` core: Flagsmith client, snapshot + `base_state_hash`, plan schema (pydantic) + canonical hash, diff, executor for the v1 ops, auto-generated rollback, audit chain + `fg audit verify`, approver keygen + `fg approve` (TTY, Ed25519), gatekeeper unix-socket daemon, rule-based planner v1 | pytest: refuse on no token / wrong hash / replay / expired / drift / non-TTY; happy path applies against local Flagsmith |
| **Sat 11/28** | Impact engine (env document + flag_engine, before/after, guardrails) + post-apply verification; `fg rollback` (pre-signed) and `fg promote` (winner from `metrics.csv` → plan with weights 100/0 or control value = winner); `fg-mcp` with read-only proxy of the official MCP; Codex/Claude Code MCP config snippets; `fg watch` panel | e2e script: prompt → plan → approve → apply → watch flips → rollback → promote, all green on local instance |
| **Sun 11/29** | Polish output, README + architecture image, record the 2–3 min demo (DEMO.md), dry-run from a clean runtime with `selfhost-up.sh`. **Hand to EL by 15:00 KST** for review/push/submission (no push from dev2) | video + repo ready, 8.5 h buffer before 23:30 |

Cut line if late: drop the tag mirror and the official MCP proxy (keep the agent → `fg-mcp` path), keep the impact preview + gate + rollback.
