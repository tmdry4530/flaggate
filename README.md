# FlagGate (Toggle Wars, Track 2: AI Agents and Feature Flags)

An agent turns a rollout/experiment request into a Flagsmith change plan with a diff and an
**impact preview computed by Flagsmith's own flag engine**. Nothing is applied until a human signs the
exact plan hash. Rollback and winner promotion go through the same gate, and everything is audited in a hash chain.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Plan schema (import `flaggate.plan`): [docs/PLAN_SCHEMA.md](docs/PLAN_SCHEMA.md)
- Demo script: [docs/DEMO.md](docs/DEMO.md)
- Local self-hosted Flagsmith (no docker/sudo): [docs/SELFHOST.md](docs/SELFHOST.md)

```bash
scripts/selfhost-up.sh      # Flagsmith v2.295.2 API on http://127.0.0.1:8600 (runtime outside repo)
scripts/seed.sh             # Admin API seed + official Python SDK read-back
scripts/fs-cli.sh flag list # official Flagsmith CLI against local instance
scripts/selfhost-down.sh
```

Runtime dir (override with `FLAGSMITH_RUNTIME`): `../flagsmith-runtime`.
No LLM API keys are used anywhere. The "brain" is the user's own coding agent over MCP, or the keyless rule-based planner.
