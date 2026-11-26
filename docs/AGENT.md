# FlagGate for coding agents: fg-mcp, fg promote, real-agent e2e

## fg-mcp (stdio MCP server)
`fg-mcp` (`flaggate/mcp_server.py`) is what a coding agent (Codex CLI, Claude Code, …) is given. It
**holds no Flagsmith key and no approver key**. Its only capability is the gatekeeper's *agent socket*
(`$FG_HOME/gatekeeper/agent.sock`), on which the gatekeeper serves no apply/rollback routes.

| tool | what it does | MCP annotations |
|---|---|---|
| `snapshot` | state summary + `target` + `base_state_hash` | read-only |
| `read_features`, `read_feature`, `read_segments` | parts of the snapshot | read-only |
| `plan_schema` | op vocabulary + template pre-filled with target/base hash | read-only |
| `plan_from_prompt` | rule-based planner in the gatekeeper → submitted plan (diff, impact, rollback) | non-destructive, closed-world |
| `submit_plan` | submit an agent-written `flaggate.plan/v1` JSON | non-destructive, closed-world |
| `plan_status` | status + result | read-only |
| `wait_for_result` | blocks ≤ 50 s per call until applied / halted / failed | read-only |

There is no apply / approve / rollback tool. "Submit" only writes a proposal into the local gatekeeper; nothing
reaches Flagsmith until a human runs `fg approve <plan_id>` in their own terminal (TTY + passphrase).

Locked by tests (`tests/test_agent.py`, `tests/test_agent_live.py`):
* the tool list is exactly the nine above; calling `apply`/`approve` returns an error;
* `import flaggate.mcp_server` loads no `flaggate.client`, `secretbox`, `approval`, `executor`, `gatekeeper` or
  `flagsmith_io` (it uses the stdlib-only `flaggate.gk_client`); `FS_*` variables are dropped at startup;
* the agent socket answers 404 to apply/rollback **even with a valid human-signed token**, and audits the attempt;
* a forward `delete_feature` from the agent is refused by the gatekeeper;
* live: no tool output contains the throw-away org's admin key, environment key or server key.

Honest limit: on a single-user box the agent runs as the same Unix user, so it could open `gk.sock` directly.
That still gets it nothing without a token signed by the approver's passphrase-protected key. For a hard
boundary run the gatekeeper as a different user and give the agent only `agent.sock`.

### Wiring
* Claude Code: `examples/claude-code.mcp.json` (project `.mcp.json`).
* Codex CLI: `examples/codex.config.toml` in a project-local/temporary config, or the same keys as `-c`
  overrides (what the e2e script does). Do not edit the global `~/.codex/config.toml` for this.
  The tool annotations make Codex's default `auto` approval mode run the tools inside `codex exec`.

## fg promote
```bash
.venv/bin/python scripts/mock_traffic.py --feature checkout_button_color \
    --rates blue=0.08,green=0.13,orange=0.09 --n 1500 --seed 7 --out /tmp/metrics.csv   # demo data only
fg promote checkout_button_color --metrics /tmp/metrics.csv      # analysis + submitted plan
fg approve <plan_id>                                             # the same human gate
```
`mock_traffic.py` evaluates synthetic, transient identities against the real Flagsmith with the official SDK (so
each visitor gets the variant Flagsmith actually serves; nothing is stored) and simulates conversions with the
given true rates. `fg promote` reads `variant,exposures,conversions` (or per-event `identity,variant,converted`),
picks the best conversion rate among variants with ≥ 100 exposures, runs a two-sided two-proportion z-test against
the control (α = 0.05) and refuses unless significant (`--force` promotes anyway and says `[FORCED]`). The plan is
`set_mv_weights` winner = 100, others 0. Rates, p-value and the metrics file sha256 go into the plan `prompt`, so
the human signs the evidence too.

## Manual steps replaced + elapsed (DESIGN §7)
`flaggate/metrics.py` counts dashboard steps per op (e.g. create segment = 2 + one per condition, segment
override = 3, MV weights = 2 + one per variant) plus one "who is affected?" check when an impact preview exists.
`fg approve`/`fg apply` print, and the `applied` audit record stores:
```
APPLIED pl_…: 2 Flagsmith calls, two-phase=True, prediction matched 60/60 (audit #3)
manual steps replaced: 8   elapsed prompt→applied: 28s
```
Elapsed is from the plan's `created_at` (when the agent's prompt became a plan) to the gatekeeper's apply.

## Real agent e2e (Codex CLI)
```bash
export PATH=$HOME/.local/bin:$PATH
env -u OPENAI_API_KEY -u UV_PROJECT_ENVIRONMENT .venv/bin/python examples/agent_e2e/run_codex_e2e.py
```
* Builds a **throw-away organisation** (deleted at the end) with 60 seeded identities; the demo org is not touched.
* Starts a gatekeeper in a temp `FG_HOME`, runs `env -u OPENAI_API_KEY codex exec --ephemeral
  --ignore-user-config -s read-only` with fg-mcp passed through `-c mcp_servers.flaggate.*` overrides
  (`~/.codex/config.toml` is neither read nor modified; its sha256 is compared before/after).
* **Demo automation only:** the human approval is played by `examples/agent_e2e/pty_approve.py`, which types the
  hash prefix and the approver passphrase into `fg approve` on a pseudo-terminal. It does not bypass the TTY
  check or the passphrase; in a real run a person types them.
* Raw logs go to `../fg-agent-e2e/<run>/` (outside the repo); a redacted copy (keys, passwords, e-mail
  addresses and the temp home replaced) goes to `docs/evidence/codex-e2e-<run>/`.
