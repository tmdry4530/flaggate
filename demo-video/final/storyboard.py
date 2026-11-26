"""Storyboard of the submission video: cards, terminal cuts and English subtitles (no narration, no TTS).

Subtitle tuple: (anchor, dt, min_duration, step label, text). `anchor` is a marker name inside the segment
(see scenes.py), or "start" / "end" of the segment; the text may use {facts} read back from the recorded output
(WHO CHANGES, prediction match, p-value, ...), so every number in a subtitle comes from the real run.
docs/VIDEO.md is generated from this file: `build.py --write-doc`.
"""
from __future__ import annotations

S1 = "1/7  THE PROBLEM"
S2 = "2/7  THE AGENT SUBMITS A PLAN (FG-MCP)"
S3 = "3/7  IMPACT PREVIEW: WHO CHANGES, AND THE GUARDRAILS"
S4 = "4/7  THE HUMAN APPROVES WITH FG APPROVE"
S5 = "5/7  APPLY, THEN VERIFY AGAINST LIVE FLAGSMITH"
S6 = "6/7  PROMOTE THE WINNER (Z-TEST)"
S7 = "7/7  ROLLBACK, THEN AUDIT VERIFY"

CARDS_BEFORE = [("title", 4.0), ("problem", 8.0), ("idea", 7.0)]
CARDS_AFTER = [("outro", 8.0)]

CARD_SUBS: dict[str, list] = {}      # the cards carry their own text


def draw_card(key: str, png, cards) -> None:
    if key == "title":
        cards.title_card(png, "FlagGate", "Let an AI agent change feature flags, without giving it the key.",
                         "Flagsmith Toggle Wars · Track 2: AI Agents and Feature Flags · demo, no narration")
    elif key == "problem":
        cards.bullets_card(png, "1/7  THE PROBLEM", "An agent that holds your Flagsmith key is dangerous", [
            "With an Admin key, an agent can change production flags on its own, and it can be wrong or be manipulated.",
            "Self-hosted Flagsmith has no approval step and no readable audit trail (checked on v2.295.2).",
            "Nobody can say afterwards what changed, or how many users it reached.",
        ])
    elif key == "idea":
        cards.architecture_card(png)
    elif key == "outro":
        cards.outro_card(png, [
            "Flagsmith Admin API, environment document, flag engine, SDK, CLI and MCP do the work.",
            "A human approves. Every change is previewed, reversible and audited.",
        ], "Open source (MIT) · self-hosted · no API keys needed · all numbers in this video are from the recorded run")


def _agent1_start(cast, _):
    return cast.marker("a_start") - 0.2


def _agent1_end(cast, _):
    t = cast.find_text(r"fg approve", after=cast.marker("a_plan_visible"))
    return t + 4.0


TERMINAL_SEGMENTS = [
    {
        "key": "agent1", "cast": "agent", "cap": 1.5, "tail": 0.5,
        "start": _agent1_start, "end": _agent1_end,
        "subs": [
            ("start", 0.4, 4.0, S2, "The agent's shell holds no secrets: no Flagsmith key, no OpenAI key."),
            ("a_fn", 0.6, 4.5, S2, "Codex CLI (signed in with ChatGPT) gets one tool server, fg-mcp: the gatekeeper's agent socket."),
            ("a_task", 0.0, 3.5, S2, "The request is plain English: 25% of pro users, and an A/B test of the button colour."),
            ("a_codex_start", 1.0, 6.0, S2, "Codex reads the flags, then asks for a plan. {plan_note}"),
            ("a_plan_visible", 0.4, 5.0, S2, "The plan is submitted. Codex cannot apply it: it tells a human to run fg approve."),
        ],
    },
    {
        "key": "human1", "cast": "human", "cap": 1.2, "tail": 3.0,
        "holds": [(r"^VERDICT:", 7.5, "v1")],
        "start": lambda c, _: c.marker("h_plan_seen") - 0.3,
        "end": lambda c, _: c.marker("h_applied") + 0.5,
        "subs": [
            ("start", 0.3, 4.5, S3, "The human's terminal. An apply through the agent's socket is refused: that door has no apply route."),
            ("h_refused", 1.0, 3.0, S3, "fg approve recomputes the diff and the impact itself."),
            ("v1", 0.0, 6.5, S3, "Who changes: {who}. Guardrail: {guard} {verdict}. Computed with Flagsmith's own flag engine."),
            ("h_review", 0.2, 5.5, S4, "The human types 4 characters of the plan hash, then a hidden passphrase: an Ed25519 signature for this exact plan."),
            ("h_applied", -4.2, 3.5, S5, "Two-phase apply: create the segment, predict again with its real id, then apply."),
            ("h_applied", -1.4, 4.5, S5, "Checked against live Flagsmith: prediction matched {match}. Manual steps replaced: {steps}."),
        ],
    },
    {
        "key": "agent2", "cast": "agent", "cap": 1.2, "tail": 0.8,
        "start": lambda c, cut: cut,
        "end": lambda c, _: c.marker("a_done") + 0.5,
        "subs": [
            ("start", 0.2, 4.5, S5, "Back to Codex: it sees the applied plan and reports the match and the time saved."),
        ],
    },
    {
        "key": "human2", "cast": "human", "cap": 1.3, "tail": 3.0,
        "holds": [(r"^METRICS ", 4.0, "v_metrics"), (r"^VERDICT:", 4.5, "v_promote"), (r"^VERDICT:", 5.0, "v2")],
        "start": lambda c, _: c.marker("h_promote_start") - 0.3,
        "end": lambda c, _: c.marker("h_audit_shown") + 0.5,
        "subs": [
            ("start", 0.5, 4.5, S6, "Later, the A/B test has traffic (simulated conversions, real Flagsmith variant assignment)."),
            ("v_metrics", 0.0, 5.5, S6, "fg promote runs a two-proportion z-test and only promotes a significant winner: {winner} wins, p {pvalue}."),
            ("v_promote", 0.0, 4.5, S6, "Promotion is a normal plan with a preview. The evidence is inside the plan the human signs."),
            ("h2_review", 0.2, 3.5, S6, "It is never pre-approved: the human signs again."),
            ("h_rollback_start", 0.3, 5.0, S7, "Incident? One command rolls back with the token approved together with the apply. No agent involved."),
            ("h_rolled_back", 0.2, 4.0, S7, "{rolled}"),
            ("h_audit_verified", 0.1, 5.5, S7, "fg audit verify checks the hash chain, and that live Flagsmith still matches the last change."),
            ("h_audit_shown", -3.0, 4.0, S7, "The log has every step: submitted, refused, approved, applied, promoted, rolled back."),
        ],
    },
]

# ------------------------------------------------------------------------------------------------ docs
def markdown() -> str:
    lines = ["# Final video: storyboard and subtitles", "",
             "Generated from `demo-video/final/storyboard.py` (`demo-video/final/build.py --write-doc`). "
             "No narration, no TTS: cards and English subtitles only. Numbers in `{braces}` are read back from the "
             "recorded output.", "",
             "## Re-record with one command", "",
             "```", "demo-video/make-final-video.sh", "```", "",
             "It builds a throw-away Flagsmith organisation on the local self-host (the demo environment is never touched), "
             "starts a gatekeeper, records two terminals with asciinema (the agent: real `codex exec` through `fg-mcp`; "
             "the human: `fg approve`, promote, rollback, audit), then cuts, subtitles and joins them with agg and ffmpeg. "
             "The MP4 and the casts land in `demo-video/final-render/` (git-ignored). "
             "`--edit-only` re-cuts the last recording without recording again; `--record-only` stops after the casts. "
             "The recording runs in a stripped environment (no inherited keys or tokens) and absolute paths are "
             "rewritten to `~/...` before they reach the screen.", "", "## Cards", ""]
    for key, secs in CARDS_BEFORE + CARDS_AFTER:
        lines.append(f"* `{key}` ({secs:g} s)")
    lines += ["", "## Terminal scenes and subtitles", ""]
    for seg in TERMINAL_SEGMENTS:
        lines.append(f"### {seg['key']} (terminal {'A: the agent' if seg['cast'] == 'agent' else 'B: the human'})")
        lines.append("")
        for anchor, dt, dur, label, text in seg["subs"]:
            lines.append(f"* `{anchor}{dt:+.1f}` **{label}**: {text}")
        lines.append("")
    return "\n".join(lines)
