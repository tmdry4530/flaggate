# FlagGate core (dev2/core): gatekeeper, executor, approvals, audit, `fg` CLI

## Modules
| module | role |
|---|---|
| `client.py` | Flagsmith Admin API client (urllib, `Api-Key`); records every call with keys redacted |
| `state.py` | `Snapshot` of project+environment, `state_hash` (= `base_state_hash`), pure `simulate(snapshot, ops)` |
| `diff.py` | machine-readable `Change` list + coloured text; archive lines say "no effect on evaluation" |
| `rollback.py` | inverse ops generated from the before-state; `rollback_plan(plan)` (pre-approved with apply) |
| `executor.py` | performs every v1 op; each step returns an undo; any failure compensates everything already done |
| `approval.py` | Ed25519 approver key (encrypted PKCS8, passphrase), signed single-use tokens bound to `plan_hash` |
| `audit.py` | append-only hash-chained JSONL (`prev_hash`/`record_hash`), `verify()` finds edits/removals/reorders |
| `secretbox.py` | gatekeeper secrets sealed with scrypt + AES-GCM |
| `hooks.py` | impact hook: `ImpactEngine` protocol + `FlagsmithImpact` (wires dev3's `compute_impact`, `repredict_with_live_ids`, `verify_live`) |
| `gatekeeper.py` | the only holder of the Flagsmith key; submit → review → apply (two-phase) → rollback; every refusal audited |
| `server.py` | gatekeeper daemon on a 0600 unix socket + `GatekeeperClient` |
| `planner.py` | rule-based prompt → Plan (rollouts, A/B, enable/disable, set value, promote, stop rollout, archive) |
| `cli.py` | `fg` |

## Gatekeeper apply checks (in order; each refusal is an audit record and HTTP 403)
token present → Ed25519 signature by the pinned approver key → action → `plan_hash` (recomputed from the stored
plan; stored file also checked against the hash recorded at submit) → environment fingerprint → expiry / max TTL
→ nonce unused → plan not already applied/halted → plan not expired → **drift**: live `state_hash` = plan's
`base_state_hash` → impact preview approvable → phase 1 (create segments) → re-predict with live segment ids
(halt + compensate if no longer approvable) → phase 2 → snapshot + live SDK verify → `applied`.
Rollback uses the pre-approved rollback token (bound to `rollback_plan(plan)`) and checks drift against the
recorded post-apply state.

`base_state_hash` covers the whole project/environment snapshot, so any unrelated change after planning also
counts as drift (strict on purpose: re-plan, it is one command).

## Honest limits
* `fg approve` refuses unless stdin and stdout are TTYs: approvals cannot be piped. A process that allocates a
  pty can still fake a TTY; the real boundary is the approver passphrase (encrypted key) and that the agent never
  holds a Flagsmith key.
* Rollback of a feature the plan created is `delete_feature` (rollback-only op: refused in submitted plans and,
  at rollback time, for any feature the forward plan did not create). Still non-reversible and shown as such on the
  approval screen: `archive_feature` (no unarchive op) and `add_variant` (rolled back to 0%, no delete_variant).
  If a rollback fails after a `delete_feature`, compensation cannot re-create the feature and says so.

## Reproduce
```bash
cd /path/to/flagsmith
env -u UV_PROJECT_ENVIRONMENT uv sync --extra dev        # project venv in ./.venv only
env -u UV_PROJECT_ENVIRONMENT .venv/bin/pytest -q         # live tests skip if :8600 is down
```
Demo (interactive terminal; `FG_HOME` defaults to `~/.flaggate`):
```bash
R=../flagsmith-runtime
fg keygen                                   # approver passphrase, twice
fg gatekeeper init --organisation-id 1 --project-id 1 --environment Development --from-env-file $R/local.env
fg gatekeeper start &                       # gatekeeper passphrase; holds the keys from now on
fg plan "roll out new_checkout to 50% of pro users"
fg apply <plan_id>                          # REFUSED (no_valid_approval)
fg approve <plan_id>                        # diff + impact + rollback, type 4 hash chars + passphrase → applies
fg audit verify && fg audit show
fg rollback <plan_id>                       # uses the pre-approved rollback token
```
The impact preview needs identities (the demo environment already has 62; with none it is NOT APPROVABLE, fail-closed):
seed them with the SDK or pass `--identities tests/fixtures/identities_200.csv` (absolute path) to `gatekeeper init`.
