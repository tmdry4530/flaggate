# Final video: storyboard and subtitles

Generated from `demo-video/final/storyboard.py` (`demo-video/final/build.py --write-doc`). No narration, no TTS: cards and English subtitles only. Numbers in `{braces}` are read back from the recorded output.

## Re-record with one command

```
demo-video/make-final-video.sh
```

It builds a throw-away Flagsmith organisation on the local self-host (the demo environment is never touched), starts a gatekeeper, records two terminals with asciinema (the agent: real `codex exec` through `fg-mcp`; the human: `fg approve`, promote, rollback, audit), then cuts, subtitles and joins them with agg and ffmpeg. The MP4 and the casts land in `demo-video/final-render/` (git-ignored). `--edit-only` re-cuts the last recording without recording again; `--record-only` stops after the casts. The recording runs in a stripped environment (no inherited keys or tokens) and absolute paths are rewritten to `~/...` before they reach the screen.

## Cards

* `title` (4 s)
* `problem` (8 s)
* `idea` (7 s)
* `outro` (8 s)

## Terminal scenes and subtitles

### agent1 (terminal A: the agent)

* `start+0.4` **2/7  THE AGENT SUBMITS A PLAN (FG-MCP)**: The agent's shell holds no secrets: no Flagsmith key, no OpenAI key.
* `a_fn+0.6` **2/7  THE AGENT SUBMITS A PLAN (FG-MCP)**: Codex CLI (signed in with ChatGPT) gets one tool server, fg-mcp: the gatekeeper's agent socket.
* `a_task+0.0` **2/7  THE AGENT SUBMITS A PLAN (FG-MCP)**: The request is plain English: 25% of pro users, and an A/B test of the button colour.
* `a_codex_start+1.0` **2/7  THE AGENT SUBMITS A PLAN (FG-MCP)**: Codex reads the flags, then asks for a plan. {plan_note}
* `a_plan_visible+0.4` **2/7  THE AGENT SUBMITS A PLAN (FG-MCP)**: The plan is submitted. Codex cannot apply it: it tells a human to run fg approve.

### human1 (terminal B: the human)

* `start+0.3` **3/7  IMPACT PREVIEW: WHO CHANGES, AND THE GUARDRAILS**: The human's terminal. An apply through the agent's socket is refused: that door has no apply route.
* `h_refused+1.0` **3/7  IMPACT PREVIEW: WHO CHANGES, AND THE GUARDRAILS**: fg approve recomputes the diff and the impact itself.
* `v1+0.0` **3/7  IMPACT PREVIEW: WHO CHANGES, AND THE GUARDRAILS**: Who changes: {who}. Guardrail: {guard} {verdict}. Computed with Flagsmith's own flag engine.
* `h_review+0.2` **4/7  THE HUMAN APPROVES WITH FG APPROVE**: The human types 4 characters of the plan hash, then a hidden passphrase: an Ed25519 signature for this exact plan.
* `h_applied-4.2` **5/7  APPLY, THEN VERIFY AGAINST LIVE FLAGSMITH**: Two-phase apply: create the segment, predict again with its real id, then apply.
* `h_applied-1.4` **5/7  APPLY, THEN VERIFY AGAINST LIVE FLAGSMITH**: Checked against live Flagsmith: prediction matched {match}. Manual steps replaced: {steps}.

### agent2 (terminal A: the agent)

* `start+0.2` **5/7  APPLY, THEN VERIFY AGAINST LIVE FLAGSMITH**: Back to Codex: it sees the applied plan and reports the match and the time saved.

### human2 (terminal B: the human)

* `start+0.5` **6/7  PROMOTE THE WINNER (Z-TEST)**: Later, the A/B test has traffic (simulated conversions, real Flagsmith variant assignment).
* `v_metrics+0.0` **6/7  PROMOTE THE WINNER (Z-TEST)**: fg promote runs a two-proportion z-test and only promotes a significant winner: {winner} wins, p {pvalue}.
* `v_promote+0.0` **6/7  PROMOTE THE WINNER (Z-TEST)**: Promotion is a normal plan with a preview. The evidence is inside the plan the human signs.
* `h2_review+0.2` **6/7  PROMOTE THE WINNER (Z-TEST)**: It is never pre-approved: the human signs again.
* `h_rollback_start+0.3` **7/7  ROLLBACK, THEN AUDIT VERIFY**: Incident? One command rolls back with the token approved together with the apply. No agent involved.
* `h_rolled_back+0.2` **7/7  ROLLBACK, THEN AUDIT VERIFY**: {rolled}
* `h_audit_verified+0.1` **7/7  ROLLBACK, THEN AUDIT VERIFY**: fg audit verify checks the hash chain, and that live Flagsmith still matches the last change.
* `h_audit_shown-3.0` **7/7  ROLLBACK, THEN AUDIT VERIFY**: The log has every step: submitted, refused, approved, applied, promoted, rolled back.

