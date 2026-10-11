# KINTSUGI-Ω: An Immune System for AI Agents

> Repairs that show their seams in gold.

KINTSUGI-Ω (Blueprint Omega) is our #neatHack 2026 entry. It is an autonomous reliability system for a fleet of AI agents, modeled on the adaptive immune system. It is built on [neatlogs](https://neatlogs.com) (senses), [Entire](https://entire.io) (memory and provenance) and [cfo.ai](https://cfo.ai) (economics).

## Mandate

> Given a fleet of AI agents and an autonomy window, keep every agent at or above its reliability SLO and at or above the fleet's gross-margin floor while an adversary attacks both the agents and the immune system itself, with zero human commits to protected branches. Produce a signed, independently checkable account of every infection, repair, prevention and economic decision.

## Status

The repository is initialised; no code exists yet. Entire is enabled from the first commit so that every AI-made change carries its checkpoint.

## Architect & AI Tooling Disclosure

Surya Prasanth R ([@suryaprasanthcse](https://github.com/suryaprasanthcse)) is the sole human architect and contributor to this repository.

KINTSUGI-Ω is built and operated with AI tooling, disclosed here for #neatHack:

- **Claude Code** writes and changes code under the architect's direction, and runs the headless agent crew.
- **neatlogs** traces the agent fleet and detects failures.
- **Entire** (with the `entire-graph` plugin) records every AI session as a checkpoint and maps the code graph.
- **cfo.ai** models the unit economics and the business plan.

AI involvement is tracked per commit, not hidden. Each AI-made commit carries an `Entire-Checkpoint:` trailer that links it to the full session transcript (`entire checkpoint explain <id>`, `entire why <file>:<line>`). Autonomous repairs also carry signed `Kintsugi-*` trailers.

#neatHack
