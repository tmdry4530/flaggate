# FlagGate demo script (target 2:40, hard max 3:00)

Setup before recording: local Flagsmith running (`scripts/selfhost-up.sh`, `scripts/seed.sh`), gatekeeper
started by the human in terminal **B** (passphrase typed off-camera), `fg watch` persona panel in pane **C**
(5 personas: alice/pro/KR, bob/free/US, chen/pro/US, dana/free/KR, eve/pro/DE + 200 sampled identities).
Screen layout: **A** = coding agent (Claude Code or Codex CLI, MCP `fg-mcp` attached) · **B** = human
terminal · **C** = live SDK panel. A small timer overlay is on.

| Time | Screen | Action / narration (voice-over) |
|---|---|---|
| 0:00–0:15 | title → C | "Flagsmith makes rollouts safe, but setting one up is 15 clicks, and handing an AI agent your Admin key is scary. FlagGate lets the agent do the work while a human keeps the final say." Panel C: everyone on old checkout, button blue. |
| 0:15–0:40 | A | Type the prompt: *"Roll out new_checkout to 10% of pro users and A/B test checkout_button_color green vs orange 50/50. Make it easy to roll back."* The agent calls `fg_snapshot` (state from Flagsmith), drafts a plan, then calls `fg_submit_plan`. Output: `plan pl_…_7f3a · hash 7f3a…`. |
| 0:40–1:05 | A (zoom) | FlagGate's diff: `+ segment pro_users_10pct (plan == pro AND 10% split)`, `+ override new_checkout ON`, `~ weights blue 34→0, green 33→50, orange 33→50`. **Impact preview (the differentiator)**: "computed with Flagsmith's flag engine on the environment document, 200 real identities: new_checkout OFF→ON for 23 (11.5%, all pro_users_10pct); button changes for 68; max exposure 11.5% ≤ guardrail 20% ✔". `manual steps replaced: 14`. |
| 1:05–1:20 | A | **Bypass attempt.** Ask the agent: "just apply it". It calls `fg_apply` with no token → `REFUSED: no valid human approval for plan 7f3a (audited #18)`. Then a forged token → `REFUSED: bad signature`. Narrate: "The agent has no Flagsmith key and can't mint an approval." |
| 1:20–1:45 | B | **Human approval scene.** `fg approve pl_…_7f3a`: it re-renders the same diff + impact itself (not from the agent), asks *"type the first 4 chars of the plan hash"* → `7f3a`, then the passphrase (hidden). Output: `✔ approved apply + pre-signed rollback (24h, single use)` → gatekeeper applies: 3 Flagsmith API calls, `prediction matched 200/200 (live SDK)`. |
| 1:45–2:00 | C + B | Panel C updates live: alice (pro) → new checkout ON, button orange; bob (free) unchanged. In B: `flagsmith evaluate --identity alice` (official CLI) shows `TARGETING_MATCH; segment=pro_users_10pct` and `SPLIT`. |
| 2:00–2:15 | B | **Rollback scene.** "Errors spike in new checkout." `fg rollback pl_…_7f3a` uses the pre-signed rollback token: no agent, no new approval. Panel C flips back within seconds. `audit #21 rolled_back`. |
| 2:15–2:35 | A → B | **Winner rollout scene.** Prompt: *"Experiment done, metrics in metrics.csv: promote the winner."* The agent reads it: green +4.1% conversion (n=2,000) → plan `set_mv_weights green 100 / orange 0`, impact "68 identities orange→green". Human `fg approve` again (promotion is never pre-approved) → applied. Panel C: everyone with green. |
| 2:35–2:50 | B | `fg audit verify` → `chain OK, 24 records, live state matches last applied plan`. Show `audit.jsonl` lines: submitted → refused → approved → applied → rolled_back → promoted. |
| 2:50–3:00 | title | "Flagsmith API, CLI and MCP do the work, a human approves, and every change is previewed, reversible and audited. FlagGate." |

Fallback if the coding agent is flaky on camera: run the same prompt with the keyless planner
`fg plan "<prompt>"`. Same plan, same gate (say so in narration).

Recording checklist: no keys on screen (env key masked in all FlagGate output; never `cat local.env`),
timer overlay, font ≥ 18 pt, rehearse twice from a clean `selfhost-up.sh` + `seed.sh` run.
