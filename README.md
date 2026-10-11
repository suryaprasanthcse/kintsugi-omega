# KINTSUGI-Ω: An Immune System for AI Agents

> Repairs that show their seams in gold.

KINTSUGI-Ω (Blueprint Omega) is our #neatHack 2026 entry. It is an autonomous reliability system for a fleet of AI agents, modeled on the adaptive immune system. It is built on [neatlogs](https://neatlogs.com) (senses), [Entire](https://entire.io) (memory and provenance) and [cfo.ai](https://cfo.ai) (economics).

## Mandate

> Given a fleet of AI agents and an autonomy window, keep every agent at or above its reliability SLO and at or above the fleet's gross-margin floor while an adversary attacks both the agents and the immune system itself, with zero human commits to protected branches. Produce a signed, independently checkable account of every infection, repair, prevention and economic decision.

## Status

Stages 1–3 of the [execution plan](docs/EXECUTION_PLAN.md) are built:

- **Proof, statistics and economics cores**: hash-chained ledger, sealed commit–reveal, Ed25519 session attestation, SPRT/McNemar/Wilson gates, the budget governor.
- **Entire and entire-graph layer**: checkpoint adapters, the motif engine, disposable attempt worktrees, headless Claude Code sessions, and the orchestrator commit protocol with audit checks C2–C6 ([spike results](docs/SPIKES.md)).
- **The fleet**: three agent hosts, [Ledger](hosts/ledger), [Scout](hosts/scout) and [Triage](hosts/triage), instrumented with neatlogs and running behind an LLM metering proxy (hard daily cap) and a seeded chaos proxy. Untrusted code runs under gVisor behind an egress allowlist. Holdouts are age-encrypted. Baselines and the Stage 3 checks are in [docs/VERIFICATION.md](docs/VERIFICATION.md).

```bash
uv sync --locked
make check                                            # ruff, pyright, pytest
uv run kintsugi fleet baseline --host ledger          # 3-seed baseline and health card
KINTSUGI_CONTRACT=1 uv run pytest tests/contract -v   # live Entire, graph, gVisor and headless checks
```

Entire is enabled from the first commit, so every AI-made change carries its checkpoint.

## Architect & AI Tooling Disclosure

Surya Prasanth R ([@suryaprasanthcse](https://github.com/suryaprasanthcse)) is the sole human architect and contributor to this repository.

KINTSUGI-Ω is built and operated with AI tooling, disclosed here for #neatHack:

- **Claude Code** writes and changes code under the architect's direction, and runs the headless agent crew.
- **neatlogs** traces the agent fleet and detects failures.
- **Entire** (with the `entire-graph` plugin) records every AI session as a checkpoint and maps the code graph.
- **cfo.ai** models the unit economics and the business plan.

AI involvement is tracked per commit, not hidden. Each AI-made commit carries an `Entire-Checkpoint:` trailer that links it to the full session transcript (`entire checkpoint explain <id>`, `entire why <file>:<line>`). Autonomous repairs also carry signed `Kintsugi-*` trailers.

#neatHack
