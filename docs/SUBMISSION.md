# FlagGate: submission text

Toggle Wars (Flagsmith × WeMakeDevs), **Track 2: AI Agents and Feature Flags**.
Demo video: **[watch on YouTube](https://youtu.be/rLrh_eTj1z8)**

Every number below comes from a file in this repository; the source is given in brackets.

## Short summary

FlagGate lets an AI agent turn a sentence like "roll out new_checkout to 25% of pro users; a/b test
checkout_button_color green vs orange 50/50" into a Flagsmith change plan with a diff and an impact preview computed
by Flagsmith's own flag engine. Nothing changes until a human signs the exact plan hash. Apply, verification,
rollback and audit all go through one gatekeeper, the only process that holds the Flagsmith key.

## What we built

* **A keyless agent front end.** `fg-mcp` is a stdio MCP server with nine tools: read state, plan from a prompt,
  submit a plan, wait for the result. It holds no Flagsmith key and no approver key, and has no apply or approve
  tool. It talks only to the gatekeeper's agent socket, which has no apply/rollback routes.
* **A gatekeeper.** It holds the Flagsmith Admin key (encrypted at rest) and checks every apply: a valid Ed25519
  signature over the plan hash, single use, not expired, no drift since planning, and an approvable impact preview.
  The apply runs in two phases: segments first, then a re-prediction with the real segment ids, then halt and
  undo if a guardrail fails. Rollback is approved together with apply. Every step is written to a hash-chained
  audit log (`fg audit verify`).
* **An impact preview.** It applies the plan to a copy of the Flagsmith environment document and evaluates
  sampled identities before and after with `flagsmith-flag-engine`. It reports who changes, per-segment
  membership, exposure versus variant shift, and guardrails. After an apply, it re-evaluates the same identities
  against the live server with the Flagsmith SDK and reports `prediction matched N/N`.
* **A human CLI.** `fg plan`, `fg approve` (TTY plus passphrase; the human sees a diff and impact that the
  gatekeeper recomputed, not the agent's summary), `fg rollback`, `fg promote` (a two-proportion z-test on
  metrics, then a plan to move the winner to 100%) and `fg audit`.

Results recorded in the repo:

* A real agent run with Codex CLI against a throw-away organisation: `prediction matched 60/60`,
  `manual steps replaced: 8   elapsed prompt→applied: 19s`
  [docs/evidence/codex-e2e-run3-20261126-122039/fg-approve.log].
* Codex's first plan call was accepted (`"first_try_success": true`) [same folder, summary.json]. The same was
  true in 3 of 3 runs [README.md, "Connecting an agent"; docs/AGENT.md]. Only the latest run's redacted log is
  committed.
* The README demo: a 25% rollout plus a 50/50 A/B test on 200 sampled identities. `WHO CHANGES: 105 of 200
  identities`, then `prediction matched 200/200` after apply and `restored base state=True` after rollback
  [README.md, "30-second demo"; demo-video/clips/readme-demo.cast].
* Test suite: 183 passed in a fresh clone with live tests against a local Flagsmith; 174 passed and 3 skipped
  without a runtime [docs/evidence/pytest-20261126.txt].

## How Flagsmith is used

| Flagsmith piece | Use in FlagGate |
|---|---|
| **Self-hosted Flagsmith v2.295.2** (open source, no Docker, user-space Postgres 16) | The whole project runs against it; `scripts/selfhost-up.sh` brings it up [docs/SELFHOST.md]. |
| **Admin REST API** | Snapshot reads; creating features, variants and segments; environment and segment-override changes via the `__future__` feature-state endpoint; deletes on rollback. Only the gatekeeper calls it. |
| **Environment document + flagsmith-flag-engine 11.1.2** | The impact preview evaluates the before and after documents for each sampled identity [docs/IMPACT.md]. |
| **Python SDK flagsmith 6.3.0** | Live verification after apply (`transient=True`, so no new identities), plus the seed read-back. |
| **Flagsmith CLI** | `scripts/fs-cli.sh` runs it against the local instance (`flag list`, `evaluate`) [docs/SELFHOST.md]. |
| **Official Flagsmith MCP server** | Probed over stdio against the local instance: 54 tools [docs/SELFHOST.md]. It needs a static Admin key in the agent's config, which is the problem FlagGate removes: the agent gets the keyless `fg-mcp` instead. |

## Track fit

Track 2 asks for AI agents working with feature flags. FlagGate lets an agent do the slow part (turning intent
into segments, splits, overrides and variant weights) and keeps a human in control of the irreversible part.

* **Uses Flagsmith:** API, flag engine, SDK, CLI and MCP; see the table above.
* **Saves time:** one sentence replaces 8 manual steps; 19 s from prompt to applied in the recorded run.
* **Human approval:** the approval is a cryptographic signature on the exact plan; the agent cannot apply.
* **Clear output:** a diff, who changes, guardrails, a verdict, verification against the live server, and a
  verifiable audit log.

## How we built it

* Python 3.11+ with uv. The Flagsmith runtime lives outside the repository, and the Flagsmith source is not
  vendored.
* Order of work: self-host and API spike → plan schema (`flaggate.plan/v1`, canonical sha256 plan hash) → core
  (state, diff, rollback, executor, Ed25519 approvals, audit chain, gatekeeper) → impact engine and live
  verification → agent front end (`fg-mcp`, keyless planner, `fg promote`) → a real Codex CLI end-to-end run.
* Live tests run against the local Flagsmith in throw-away organisations, which are created and deleted per test
  (`tests/live_helpers.py`).
* Known limitations are listed in README.md ("Security model"): the TTY check can be faked by a pty (the real
  boundary is the encrypted approver key plus passphrase), a same-user attacker with root or a keylogger is out
  of scope, and the preview is a prediction on a sample.
