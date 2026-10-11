# KINTSUGI‑Ω Execution & Build Plan

| Field | Value |
|---|---|
| Status | **Approved by the Architect** (Surya Prasanth R, @suryaprasanthcse), 2026‑10‑11 |
| Specification | `../Blueprint_Omega_Master.md` (v1.0, full version). This plan reconciles it with the verified environment; it does not dilute it. |
| Visual archive | `../kintsugi-omega-blueprints/kintsugi-omega-blueprint-img_{1..12}.png` |
| Scope | Full Blueprint Omega. No reduced cut. |

This file is the in-tree source of truth for every interactive or headless session. Amendments discovered during execution are appended to the **Amendments log** at the end, never edited into the approved text.

---

## 0. Schedule constraint

- Plan approval: 12:27 IST, Oct 11. Submission window closes 23:55 IST, Oct 12 (about 35½ h).
- The specification itself estimates about 4 engineer-weeks for the full build (§13.2 R1, "Certain"). The estimate for Stages 1–13 below is 45–50 h of continuous wall-clock work.
- The autonomy window W must end by about 19:55 IST Oct 12 to leave about 4 h for reveals, audit, video and submission. Therefore **T0 ≤ 19:55 IST Oct 12 − W**.
- W and T0 are decided at the Stage 8 go/no-go gate (ruling D1).

## 1. Verified environment baseline (pre-Stage 1)

| Area | Verified state |
|---|---|
| Entire | CLI 0.11.4 and entire-graph v0.4.0 (`no_egress: true`). Checkpoints are per-checkpoint refs `refs/entire/checkpoints/<last-2-chars>/<ULID>`, not the legacy `entire/checkpoints/v1` branch. No `rewind` command exists. Available: `entire checkpoint explain --json/--transcript`, `entire why <file>:<line> --json`, `entire session resume/stop`. |
| Entire behaviour | A mid-turn commit is linked only when the files were edited with Claude Code's Edit/Write tools. Each Stop hook adds a "Finalize transcript" commit, which moves the checkpoint ref. The pre-push hook only pushes refs in its local queue. Built-in redaction blanks secret-looking lines even with zero configured rules. |
| Windows | Entire's `prepare-commit-msg` blocks on a hidden `CONIN$` prompt unless `ENTIRE_TEST_TTY=0`. The Claude desktop app is MSIX-packaged and virtualizes `%APPDATA%`/`%LOCALAPPDATA%` writes, so tooling lives under `~/.local/share` (`UV_PYTHON_INSTALL_DIR`, `ENTIRE_PLUGIN_DIR`). |
| Tools present | `claude` 2.1.269 (headless CLI; the desktop app runs 2.1.295), Node, npm, git 2.55. Docker Desktop installed, engine stopped. WSL2 distros Ubuntu and docker-desktop (stopped). |
| Tools missing | `temporal`, `gh`, `make` (Windows), `age` CLI (the `pyrage` library replaces it). |
| Python | uv-managed CPython 3.12.14, pinned by `.python-version`. |
| Commit policy | Sole author and committer: Surya Prasanth R `<suryaprasanthcse@gmail.com>`. Never `Co-authored-by:`. Attribution only through `Entire-Checkpoint:` and `Kintsugi-*` trailers. |

## 2. Spec-to-reality reconciliation

### (a) No `rewind`: L1, L2, L3

- **L1 (failed hypotheses).** Rewind means *discard the worktree, keep the checkpoint* (spec §4.3.4, honest scope). Every attempt therefore commits, including attempts that will fail the gates, because a checkpoint exists only for a commit. The Surgeon session edits `wt/INC/Hk`; after the session ends the orchestrator signs and commits on `kintsugi/INC/Hk`; Entire links the commit to the session. The attempt is pinned as `refs/kintsugi/attempts/INC/Hk/<n>` and pushed with the checkpoint refs; then `git worktree remove --force`. Attempt commits stay reachable (never garbage-collected) and audit checks C2–C5 can verify them.
- **L2 (process killed mid-session).** A headless session killed before its Stop hook has no shadow branch and no checkpoint (A14). Recovery: adopt a commit whose nonce matches the ledger; otherwise kill the process group, `entire session stop <id>`, `git reset --hard` + `git clean -fdx` inside the disposable worktree only, hash the Claude Code transcript (`~/.claude/projects/.../<sid>.jsonl`) into the bundle as evidence of the abandoned attempt, write an `abandoned` ledger entry, and start attempt 2 with a **new nonce**. Long-lived in-place sessions (Immunologist refinement, orphan recovery) use `entire session resume <branch>` plus `claude -p --resume <sid>` (spike N7).
- **L3 (wrong fix in production).** Reverting is git (`git revert -m 1` → PR → expedited gates → merge), not Entire. Because audit check C4 requires a checkpoint for **every** commit in W, reverts, L5 configuration treatments and eval-augment commits run inside a templated **headless notary session**: a Claude Code session whose prompt contains the nonce and performs exactly that operation.

### (b) Audit check C4 and the Archivist's Intent Record

**C4/C5**, for every commit in W on protected branches plus attempt refs:

1. Require both the `Kintsugi-*` trailers and an `Entire-Checkpoint:` trailer.
2. The ref `refs/entire/checkpoints/<id[-2:]>/<id>` must exist on origin. Offline mode reads a `git bundle` of these refs.
3. `entire checkpoint explain <id> --json`: `files_touched` must cover the files in the diff.
4. `entire checkpoint explain <id> --transcript`: the nonce must appear verbatim and not on a redacted line.
5. Cross-check: `entire why <file>:<line> --json` on sampled changed lines names the same checkpoint and commit.
6. **C5:** the session's first user message, canonicalised and SHA-256'd, equals `Kintsugi-Prompt`.

**Intent Record:** `entire graph search` → `git blame` → resolve the checkpoint two independent ways (`entire why --json` and the commit trailer; they must agree) → read the transcript → select the turn(s) whose Edit/Write tool calls touched the blamed file (the "Intent" field is ignored: it shows the first prompt in the checkpoint's scope) → the Archivist (Opus) writes clauses with verbatim quotes → the Auditor checks every quote against the same transcript bytes, whose SHA-256 is recorded. `git bisect` steps map to checkpoints through trailers; commits without one are labelled `no-checkpoint`. The honeytoken check (§7.2) runs `git grep` over **all** `refs/entire/checkpoints/*` trees.

### (c) Motif engine on entire-graph v0.4.0 (Python)

Python relations: `CALLS`, `ASYNC_CALLS`, `CONSTRUCTS`, `CONTAINS`, `DATA_FLOWS`, `DEFINES`, `IMPORTS`, `EXTENDS`, `INHERITS`, `OVERRIDES`, `PARAM_TYPE`, `RETURNS_TYPE`, `USES_TYPE`, `HANDLES_GRAPHQL`, plus the heuristic relations of the `full` profile (`HTTP_CALLS`, `HANDLES_TOOL`, `TESTS`, `FILE_CHANGES_WITH`). There is no `READS_FIELD`, `WRITES_FIELD`, `ACCESSES` or `IMPLEMENTS` for Python.

- **Motif v1.0:** nodes `R[is_retry_wrapper]`, `Enc` (enclosing function), `F` (the retried scope: nested function, lambda, or the decorated function itself), `S[has_mutable_accumulator]`; edges `[Enc,R,CALLS]`, `[Enc,F,CONTAINS]`, **`[F,S,CONSTRUCTS]`**.
- **Motif v1.1** adds `T[is_side_effecting]` and `[F,T,CALLS*]`, where `CALLS*` is a derived transitive-closure relation computed before VF2 matching (VF2 matches edges exactly).
- **`has_mutable_accumulator`:** `entire graph def S` (fields and methods) plus a deterministic stdlib `ast` scan for list/dict/set fields mutated in methods; Haiku decides only when the scan is inconclusive, and its verdict is cached by (symbol, blob SHA).
- **`is_side_effecting`:** `CALLS*` reaches a tool annotated `side_effect: true` in the host's tool registry, or an `HTTP_CALLS` POST/PUT/DELETE.
- Validation is unchanged: match before the fix, no match after it, precision ≥ 0.9 on the healthy fleet. The snapshot record schema is pinned with golden files (A9).

### (d) `ENTIRE_TEST_TTY=0` everywhere

- Build commits: `.claude/settings.json` `env` block and the CLAUDE.md commit rules.
- Runtime: one subprocess gateway, `kintsugi/sandbox/proc.py`, builds every child environment from an allowlist and forces `ENTIRE_TEST_TTY=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_EDITOR=true`, with timeouts and process-tree termination (root first, so a killed `git commit` aborts instead of completing without a trailer). An architecture test and a ruff banned-API rule forbid `subprocess` anywhere else.
- Headless sessions get the environment from the gateway plus `--settings` (which also sets commit attribution to `""`).
- Every commit is verified afterwards (trailer + checkpoint ref). On failure the activity fails, the disposable worktree is reset and nothing is pushed.
- Entire 0.11.4 and entire-graph 0.4.0 are pinned; a contract test runs the hook on a scratch repo under a timeout to catch regressions.

### (e) Further reconciliations

1. **Redaction vs. the proofs (critical, spike N1).** Entire redacts secret-looking lines. A hex nonce or random-looking honeytoken could be blanked: C4 would fail and, worse, C6's "0 honeytoken hits" would pass trivially. Nonces and honeytokens are therefore **passphrase-formatted**; Stage 2 adds a byte-exact prompt round-trip test and a **positive control** proving C6 finds a planted honeytoken.
2. **Parallel Surgeons (N5).** K concurrent sessions in K worktrees: each commit must link to its own session.
3. **Ref syncing.** Checkpoint refs are pushed explicitly after each commit and again after finalize; audit clones fetch `refs/entire/*`.
4. **Public transcripts.** Checkpoint transcripts are pushed with the code: add redaction rules for Anthropic and neatlogs key patterns and a secret scan of checkpoint trees before every push (N6).
5. **Platform.** gVisor (§5.10) needs Linux: the runtime for Stage 3+ and the autonomy run lives in WSL2 Ubuntu (ruling D2). The 24 h run is never launched from inside the Claude desktop app.
6. **No co-author trailers.** The orchestrator's commit validator rejects `Co-authored-by:`; headless sessions get commit attribution set to `""`.

## 3. Architect rulings D1–D8

| # | Decision | Ruling |
|---|---|---|
| D1 | W length and T0 | Deferred to the Stage 8 go/no-go gate, once the C1–C14 mutation checks are green. |
| D2 | Runtime platform | Stages 1–2 stay cross-platform through `kintsugi/sandbox/proc.py` so local Windows execution works. Prepare WSL2 Ubuntu + Docker/gVisor for Stage 3+ and the autonomy run. |
| D3 | Repo layout | Build the hosts (`hosts/ledger`, `hosts/scout`, `hosts/triage`), the antibody library (`antibodies/`) and the sabotage log (`sabotage-log/`) inside the `kintsugi-omega` monorepo first, so `suryaprasanthcse/kintsugi-omega` is self-contained and runnable from a single clone. Remote repo adapters are configurable once the GitHub token is provided. |
| D4 | Commit identity | `Surya Prasanth R <suryaprasanthcse@gmail.com>` is the sole git author and committer on all commits. Never a bot contributor, never `Co-authored-by:`. Machine origin is proven by Ed25519 signatures (`.kintsugi/attest.pub`), `Entire-Checkpoint:` trailers and `Kintsugi-*` trailers. |
| D5 | Ledger location | An unprotected, append-only `ledger` branch for ledger entries and anchors. |
| D6 | Temporal deployment | Implement both the deterministic `sqlite_fsm.py` WAL state machine and the Temporal workflow/activity definitions, using the single-binary Temporal CLI dev server or Docker Compose when started in Stage 5. |
| D7 | Headless Claude billing | The existing Claude Code login for Stage 2 headless spikes; `ANTHROPIC_API_KEY` with a hard daily dollar cap in `kintsugi/sandbox/llm_proxy.py` and `kintsugi/econ/governor.py` from Stage 3/6. |
| D8 | Parallel build lanes | Approved where safe, provided every commit is made cleanly with `ENTIRE_TEST_TTY=0`, carries a verified `Entire-Checkpoint:` trailer, and credits lines accurately. |

## 4. Stages

"Rehearsal" means an unsealed, clearly labelled practice run before T0.

### Stage 1 — Foundation and deterministic cores (no credentials)

- **Builds:** `pyproject.toml`/`uv.lock`, ruff, pyright, `Makefile` (judge, audit, demo, fleet-up/down, check), `.github/workflows/omega-ci.yml`, `kintsugi/cli.py`, `kintsugi/sandbox/proc.py`, `kintsugi/proof/{canonical,ledger,sealed,attest,bundle}.py`, `kintsugi/stats/{mcnemar,sprt,power,holm,wilson,bootstrap}.py`, `kintsugi/econ/{governor,wright}.py`, `kintsugi/memory/{antibody,motif}.py`, `kintsugi/control/states.py`, `schemas/E1–E6`, `.kintsugi/protected_paths.yaml`.
- **Commits (~7):** scaffold + plan · proc gateway · proof primitives · stats · econ · memory/motif core · state machine, schemas, protected paths.
- **Verified by:** SPRT boundaries 2.890/−2.251, steps +0.6931/−0.0660, accept after 35 clean runs, reject at n=8 (LLR 3.268), accept at n=46 (LLR −2.275); Connor n=34; McNemar b=26, c=1 → p≈2.09e-7; governor reproduces §6.2 (Pool $71.70, Cap/B $12.40) and §6.8 (B $4.83, T1 $2.34, GM 78.6%); Wright fit recovers b; RFC 8785 vectors; ledger tamper detected at the exact seq; commit-reveal and Ed25519 round-trip; property test: every state path terminates. Spike N3 is checked on the first commit.
- **Invariants:** model-level I3; I4 formulas; I1/I2/I7 primitives. **Cascade/proof:** SPRT core (L3), L5 math, building blocks for C1, C3, C6.

### Stage 2 — Entire, graph, git and attestation layer + headless spikes

- **Builds:** `kintsugi/memory/{entire,graph}.py` (every subcommand, JSON adapters, golden fixtures), `kintsugi/sandbox/worktree.py`, `kintsugi/crew/claude_session.py` (`claude -p --output-format stream-json --allowedTools … --settings …`, nonce injection, cost and transcript capture), `kintsugi/proof/{commit,redaction}.py`, `tests/contract/test_entire_*.py`.
- **Spikes (contract tests on scratch repos):** A1/N2 (does Entire link a commit made after `claude -p` exits?), N1 (redaction vs nonce/honeytoken/prompt formats), N5 (three concurrent worktrees), A14 (`kill -9` mid-session), N4 (hooks inside worktrees), A9 (snapshot schema).
- **Commits:** ~6. **Verified by:** C4/C5 pass on real checkpoints and fail on mutated ones; `entire why` agrees with the trailer mapping; the C6 positive control finds a planted honeytoken.
- **Invariants:** I2 machinery; I6 detectability; I5 transcript-scan tooling. **Cascade/proof:** L1 attempt checkpoints, L2 abandoned-session handling, C2–C6.

### Stage 3 — Fleet: Ledger, Scout, Triage + sandbox and proxies (monorepo, D3)

- **Builds:** `hosts/ledger` (`ReconcileSession`, `run_reconcile`, `util/retry.py`, tools `parse_invoice`, `lookup_vendor` with a default argument, `fx_rate`, side-effecting `post_journal_entry`, `fmt_amount`; runtime with auto-deploy from main; 120-invoice generator 80/40; exact-match grader; `test_resume_after_partial_batch`), `hosts/scout` (BM25 frozen corpus; the disclosed look-alike per-attempt scratch buffer; 40/20), `hosts/triage` (mock GitHub, `config/routing.yaml`, mixed-language issues; 70/30), `kintsugi/sandbox/{runner,llm_proxy,chaos_proxy,cassette}.py`, age-encrypted holdouts, `.kintsugi/holdout.sha256`, per-host CI and a reusable G7 workflow, `grader.verdict` spans. WSL2 + Docker + gVisor integration (D2). Remote repo adapters stay configurable (D3).
- **Commits:** ~12. **Verified by:** baselines at 3 seeds ≥ SLO with Wilson CI; Ledger handles 8% background 429s with no duplicate lines; deterministic cassette replay; chaos proxy identical per seed; proxy enforces budgets and meters cost within 1%; sandbox blocks non-allowlisted egress; `neatlogs doctor --probe`; holdout canary strings for leak scans.
- **Invariants:** I5 (encryption), I4 (proxy caps). **Cascade/proof:** code shapes for S‑17 (L0), S‑19 targets (L4), the Scout look-alike (L3), Triage Pilot on Haiku (L5).

### Stage 4 — Senses: neatlogs, receptors, ingress, poller, Fixes

- **Builds:** `kintsugi/senses/{neatlogs_api,detections,ingress,poller,tags,fixes_mcp}.py`, receptors R‑001…R‑007 per host, SQLite dedupe store, webhook registration. **Spikes:** A3–A7, A11–A13.
- **Commits:** ~6. **Verified by:** contract tests against the pinned OpenAPI; duplicate events dropped with 200; ingress answers within 2 s; poller recovers hits during a simulated outage; replayed deliveries suppressed; I7 completeness function on fixtures.
- **Invariants:** I7. **Cascade/proof:** L0 detection, L2 poller and replay, C8, C9.

### Stage 5 — Control plane: Temporal + SQLite-WAL + Watchdog (D6)

- **Builds:** `kintsugi/control/workflows.py` (8 workflows), `activities.py` (all §5.3.2 activities with timeouts, heartbeats, retries, idempotency keys), `worker.py` (separate crew, Surgeon, senses pools), `sqlite_fsm.py` (§5.4 resume algorithm, single-writer socket), `watchdog.py` (supervision, health checks, authenticated chaos API, dead-man switch).
- **Commits:** ~6. **Verified by:** time-skipping tests for every workflow; replay-determinism tests; `kill -9` drill (heartbeat timeout → attempt 2 adopts by nonce or cleans up with a new nonce) in both Temporal and SQLite modes; `continue_as_new`; all signals and queries.
- **Invariants:** I3, I1 ordering. **Cascade/proof:** L2, L4 signal path, T8.

### Stage 6 — Crew, primary incident loop, gates G0–G7, ship (L0, L1, L4)

- **Builds:** `kintsugi/crew/{sentinel,pathologist,archivist,coordinator,surgeon,auditor}.py` with hashed prompt templates, `kintsugi/senses/{signature,drift}.py`, `kintsugi/gates/g0…g7`, context manifests, `kintsugi/control/ship.py` (issues, PRs with the E.4 receipt, merges), the protected-path pre-commit hook, `.github/workflows/g7-gates.yml` (holdout key in an Actions secret; logs print aggregates only).
- **Commits:** ~12. **Verified by a full Ledger rehearsal:** G0 fails 3/3 → Intent Record with verified quotes → three parallel Surgeons, each with a checkpoint → round 1 fails and is rewound → round 2 → pre-registered dispute experiment → ship → G7 green → merge. **Negative tests:** protected-path edits blocked twice; diffs over 120 lines blocked; new outbound network calls blocked; zero holdout leaks across crew spans and transcripts; the Auditor never sees Surgeon output; G6 stops a round when the budget is spent. **L4 drill:** a mid-round drift push → rebase → reverify record.
- **Invariants:** I1, I3, I5. **Cascade/proof:** L0, L1, L4; C2–C5, C7; T2, T5.

### Stage 7 — Canary, antibodies, motif engine, vaccination, quarantine (L3)

- **Builds:** `kintsugi/gates/canary.py`, `kintsugi/control/revert.py`, `kintsugi/memory/{motif,predicates,library,contrastive}.py`, `kintsugi/crew/immunologist.py` (synthesis, receptor generate/preview/create/activate, backfill, maturation, vaccination, quarantine, refinement), the `antibodies/` library in the monorepo (D3), the trace-to-test eval-augment path.
- **Commits:** ~10. **Verified by:** motif validation (match before, not after, precision ≥ 0.9); induced-trigger rule; receptor preview precision ≥ 0.9; a backfill job completes; L3 rehearsal (Scout canary reject → revert PR → reverse canary → `pause_antibody` → comments on open PRs → QUARANTINED with the receptor still active); contrastive learner separates 3 positives from Scout; v1.1 motif excludes Scout; consistent lineage; secondary responses measured far cheaper than primary.
- **Cascade/proof:** L3, L0 steps 11–14, S5, C11, T9.

### Stage 8 — Proof plane: Saboteur, sealing, ledger anchors, `kintsugi audit`

- **Builds:** `kintsugi/crew/saboteur.py` (catalog enums, strict-schema generation, realism rules + rubric ≥ 0.7, sealing, age storage, upstream-lane delivery, chaos API), `kintsugi/proof/{sealed,ledger anchors,attest(KMS option),bundle,audit}.py`, `sabotage-log/` in the monorepo (D3), anchors on the `ledger` branch (D5), `make judge`.
- **Commits:** ~10. **Verified by a mutation suite:** every check C1–C14 passes on a clean fixture and fails on a tampered one (reordered ledger, missing trailer, forged signature, missing nonce, prompt mismatch, honeytoken positive control, missing G7 run, unknown trace ID, unmatched hit, cost Δ over 1%, lineage gap, ruleset gap, changed holdout hash, protected self-repair path touched). Offline mode needs no credentials; exit codes 0/1/2.
- **Invariants:** I2, I6, I7. **Cascade/proof:** sealing S‑17/18/19; T1, T3, T4, T8, T10. **Go/no-go gate for D1.**

### Stage 9 — Economic cortex and cfo.ai (L5)

- **Builds:** `kintsugi/econ/{governor (live), actuals, wright (nightly refit), cfo_sync}.py`, `business/{plan.json, actuals.json, actuals.csv, cfo_export/}`, the L5 treatment path with its decision span.
- **Commits:** ~5. **Verified by:** plan hash pinned per incident; C10 within 1%; the governor never lowers power below 0.8 and escalates instead; L5 rehearsal on Triage; cfo.ai model with Unit economics / Runway / Scenarios tabs and plan-vs-actual.
- **Invariants:** I4. **Cascade/proof:** L5, T7.

### Stage 10 — Onboarding O1–O7, Repair Clinic, open-source hosts

- **Builds:** `kintsugi/onboard/{intake,instrument,eval_synth,baseline,immune_scan,replay_mode}.py`, the Onboarding workflow, a Clinic intake issue form with consent checkboxes, PR-only receipts for foreign repos.
- **Commits:** ~6. **Verified by:** OSS‑1 onboarded end to end in the sandbox; replay mode from exported traces; code refuses to merge into a foreign repo.
- **Cascade/proof:** maturation and vaccination targets, T9, building in public.

### Stage 11 — Arena and public API

- **Builds:** `arena/` (Next.js: fleet board, incident stream, immunity matrix, learning curve, ledger head, "Sabotage it"), `kintsugi/arena_api.py` (strict-schema Haiku compiler, sealing, immediate commitment), rate limiting, daily cap.
- **Commits:** ~6. **Verified by:** injection fuzz corpus compiles only to catalog values or is rejected; 1 request per IP per 10 minutes; escalations displayed.
- **Cascade/proof:** T6, demo, building in public.

### Stage 12 — Self-repair (INC‑SELF‑01)

- **Builds:** `hosts/omega` (replay suite of 20 incidents recorded in Stages 6–9 rehearsals), grader (root-cause hit@3, cost, p95), enforcement of the self-repair protected scope (C14).
- **Commits:** ~4. **Verified by:** a self-repair rehearsal with before/after trace sets; C14 blocks any diff touching proof, stats, gates or attestation code.
- **Cascade/proof:** §6.9, the portal's "agent improved" row.

### Stage 13 — Integration, chaos drills, dress rehearsal, T0 preconditions

- Compressed L0–L5 + self-repair dress rehearsal; `kintsugi audit --all` green.
- T0 checklist (§6.1): baselines recorded; background chaos profile on; holdout hashes committed; attestation key generated under Architect custody, public key committed to `.kintsugi/attest.pub` and posted; genesis hash; rulesets active; S‑17/18/19 sealed; runtime outside the Claude desktop app with keep-awake.
- Omega's own code is frozen at T0; after that only Omega's self-repair loop may change it.

### Stage 14 — Autonomy window W, then submission

- During W: hourly anchors (human-posted); Clinic tracked.
- After W: reveals, `kintsugi audit --all`, published bundles, `make judge`, the §8 video recorded from the real run, the X quote between 7:00 and 11:55 PM IST on Oct 12.

### Coverage map

| Item | Stages |
|---|---|
| L0 | 3, 4, 6, 7, 8 |
| L1 | 2, 6 |
| L2 | 4, 5, 8 |
| L3 | 1, 7 |
| L4 | 3, 6 |
| L5 | 1, 9 |
| Self-repair | 12 |
| C1–C14 | 8 (built from Stages 1–7) |
| I1 | 5, 6 |
| I2 | 2, 8 |
| I3 | 1, 5 |
| I4 | 3, 9 |
| I5 | 3, 6 |
| I6 | 2, 8 |
| I7 | 4, 8 |

## 5. Credentials, decisions and spikes

### Credentials and accounts

| Input | Needed by | Can proceed without it |
|---|---|---|
| `NEATLOGS_API_KEY` (SDK) | Stage 3 | Host code and offline tests |
| neatlogs service-account token + project UUID (Public API) | Stage 4 | Client against the pinned OpenAPI with fixtures |
| neatlogs alert rules in the UI (A13) + public ingress URL | Stage 4 | Poller path |
| neatlogs Fixes MCP (A4) | Stage 6 | Loop runs without the prior |
| `ANTHROPIC_API_KEY` with a spend cap | Stage 3 (proxy), Stage 6 (crew) | Stage 2 spikes use the existing Claude login (D7) |
| GitHub token or App; `gh` installed | Stage 3 (remote adapters), Stage 6 (ship), Stage 13 (rulesets) | Stages 1–2 |
| Signing: AWS KMS `ECC_NIST_P256` + CloudTrail, or local Ed25519 | Stage 8 | Ed25519 from Stage 1 |
| cfo.ai account + model + Ari MCP (A8) | Stage 9 | Governor math |
| AWS (S3 object lock, Fargate, SQS, dead-man Lambda), optional | Stages 4/8/13 | Local equivalents |
| Docker engine in WSL2 Ubuntu + gVisor; `temporal` CLI; Node | Stages 3, 5, 11 | — |
| Crustdata key (optional) | Stage 3 | Optional |

### Spikes

| Spike | Stage | Status at approval |
|---|---|---|
| A1/N2 headless hooks and post-exit commit linking | 2 | Open |
| A2 checkpoint data readable programmatically | — | Resolved |
| A10 graph supports Python semantically | — | Resolved |
| A3, A5, A6, A7, A11, A13 (neatlogs behaviour) | 4 | Open |
| A4 Fixes MCP | 6 | Open |
| A8 cfo.ai MCP | 9 | Open |
| A9 snapshot schema | 2/3 | Open |
| A12 backfill bounds | 7 | Open |
| A14 abandoned sessions | 2 | Open |
| A15 RFC 8785 library | 1 | Open |
| N1 redaction vs nonce/honeytoken/prompt (critical) | 2 | Open |
| N3 stale `last_checkpoint_commit_hash` after the history rewrite | 1 | Open |
| N4 hooks in worktrees | 2 | Open |
| N5 concurrent sessions in parallel worktrees | 2 | Open |
| N6 secret scan of public transcripts | 2 | Open |
| N7 `entire session resume` + `claude --resume` | 5 | Open |

Stage 2 results are recorded in `docs/SPIKES.md` (A9, N1, N4, N5 and A14 mechanics resolved; the
model tier of A1/N2, N1, N5 and A14 waits on A-20).
| N8 gVisor in WSL2 | 3 | Open |
| N9 rulesets and merge identity | 6/13 | Open |

### Do now, independent of code

Repair Clinic intake starts now (§15.2): third-party merges need lead time and are the strongest proof (★★★★★). Posting is the Architect's; the intake form lands in Stage 10.

---

## Amendments log

Entries are appended in order as execution discovers them. Each states what changed and why.

| # | Stage | Amendment | Why |
|---|---|---|---|
| A-1 | 1 | Entire's line attribution is binary: lines from Claude Code's Edit/Write tools are `agent_lines`; everything else, including tool-generated files such as `uv.lock`, is `human_added`. Lockfiles are always produced by tooling, never hand-written. | Commit 1 showed 511 agent + 595 "human" lines; the 595 are exactly `uv.lock`. The authorship claim rests on attestation and checkpoint linkage, not on Entire's agent percentage. |
| A-2 | 1 | The attestation digest realises `‖` as the RFC 8785 encoding of the six fields as a JSON array. | Plain concatenation is ambiguous at field boundaries (`"ab"‖"c"` = `"a"‖"bc"`). |
| A-3 | 1 | Nonces are 128-bit, 11-word passphrases; honeytokens are 72-bit, 6-word passphrases (`kintsugi/proof/passphrase.py`). Hex nonces are refused. The spec's E.6 hex example is superseded. | Spike N1 format. Survival through Entire's redaction is verified in Stage 2. |
| A-4 | 1 | The T0 attestation key is generated by the Architect with `kintsugi keys generate` outside any repository; only `.kintsugi/attest.pub` is committed, at T0 (Stage 13). No key exists in the repo before then. | Key custody (§4.5.4): the private key must never enter a repository or an agent's context. |
| A-5 | 1 | Four I3-closure transitions extend the §5.4 table: REPRODUCED→ESCALATED (no suspect), ATTRIBUTED→ESCALATED (zero budget), SHIPPED→ESCALATED (merge blocked), CANARY→ESCALATED (revert failed or re-entry limit). Canary re-entries are bounded (default 2). | Without them an incident can stop in a non-terminal state, violating I3; `tests/control/test_states.py` proves both the gap and the closure. |
| A-6 | 1 | RETIRED is reachable from CANDIDATE, MATURED and QUARANTINED, not only ACTIVE. | An antibody that fails maturation or refinement must be closable. |
| A-7 | 1 | E.2, E.5 and E.6 schemas require real 64-hex hashes, `sha256:` digests and passphrase nonces; the spec's symbolic examples (`h_811`, `9a41…`, `9f2c4e…`) are illustrative and fail them by design. E.4 prose placeholders are normalised to identifiers in `schemas/E4_repair_receipt.template.md`. | Schemas are the audit contract and must reject placeholders. |
| A-8 | 1 | Motif kind `function` accepts snapshot kinds `function`, `method`, `lambda`, `closure` until spike A9 pins the real snapshot kinds. | The kind vocabulary of compact-ndjson snapshots is not yet observed. |
| A-9 | 1 | Helper modules beyond the Stage 1 file list: `kintsugi/proof/passphrase.py`, `kintsugi/proof/schemas.py`, `kintsugi/gates/protected_paths.py`, `typings/pyrage` stubs, `tests/fixtures/spec/` (verbatim Appendix E examples). | Implementation support for A-3, A-7, the protected-path policy, and pyright on the compiled `pyrage` module. |
| A-10 | 2 | entire-graph v0.4.0 has no `context`, `callers` or `callees` subcommands. `kintsugi/memory/graph.py` provides them as `search --format agent`, `neighbors --direction in` and `neighbors --direction out`. | Verified: the plugin answers "unknown command". |
| A-11 | 2 | `entire doctor` is an interactive fixer and is never invoked by Omega. Health checks use `entire graph doctor --json` and `entire status`. | Verified from its help text: it "offers to fix" session state. |
| A-12 | 2 | Gateway hardening: npm `.cmd` shims are resolved to their `.exe` and batch files are refused; stdin is written as exact bytes; long prompts go in on stdin from a file. | Real Windows bugs: a shim would pass prompt text to `cmd.exe` (`&`, `|`, `>`), text-mode stdin rewrote `\n` as `\r\n` (corrupting `git commit -F -` and `git mktree`), and command lines are capped near 32k characters. |
| A-13 | 2 | entire-graph snapshot schema 1.1 is pinned with golden files. The graph also credits a closure's calls and constructions to its enclosing function, so the motif binds the retried scope `F` through `CONTAINS`, never through the enclosing function. | Spike A9; the reconciled v1.0/v1.1 motifs are validated on real snapshots. |
| A-14 | 2 | A checkpoint ref's `i/content_hash.txt` is `sha256(i/full.jsonl)`; offline audits read checkpoints with plain git and verify that hash. | Verified on real checkpoints. |
| A-15 | 2 | `.entire/settings.json` carries `redaction.custom_redactions` (a name → RE2 map; Entire rejects list forms): Anthropic, neatlogs, bearer, GitHub, AWS, age and private-key rules, plus the permanent `kintsugi_redaction_probe` positive control. The same rules drive a pre-push scan of checkpoint trees. | Spike N1: rules load live (`inline_patterns: 8`), the probe is redacted, passphrase tokens survive, a base64 decoy is blanked by Entire's built-in scanners. |
| A-16 | 2 | `orchestrator_commit` refuses staged changes that the session's Write/Edit tool calls do not explain (`session_paths`). | A real checkpoint did not record a file written by a shell command; C4's `files_touched` clause caught it. Refusing before commit keeps C4 green by construction. |
| A-17 | 2 | `Kintsugi-Prompt` is `sha256` of the exact UTF-8 bytes of the session's first prompt, which is delivered on stdin; the attestation's `prompt_template_sha` field carries the same digest. | Makes C5 a byte-exact check. |
| A-18 | 2 | Headless sessions run with `--setting-sources project,local`, `--strict-mcp-config`, an orchestrator-chosen `--session-id` and `--max-budget-usd`. | A headless session otherwise loads the user's plugins and skills; a pre-chosen session ID lets L2 adopt an attempt by ID. |
| A-19 | 2 | Checkpoint-ref sync is judged by the remote's final state, never force-pushes, and scans for secrets before pushing. | Entire's pre-push hook races explicit ref pushes ("cannot lock ref … expected"); a diverged ref may be evidence. |
| A-20 | 2 | The model tier of spikes A1/N2, N1, N5 and A14 waits on a logged-in headless CLI (`claude auth login`) or the Stage 3 `ANTHROPIC_API_KEY`. The tests exist and skip with that reason. | The desktop app's auth is host-scoped; it is not borrowed for unattended processes. |
| A-21 | 2 | Commit trailers are parsed across consecutive trailing trailer-only paragraphs. | A hook may append `Entire-Checkpoint:` in its own paragraph after the `Kintsugi-*` block. |
| A-22 | 3 | Architect ruling: the model tier of spikes A1/N2, N1 (headless), N5 (commit linkage) and A14 (mid-tool kill) is carried into Stage 3 and runs once a model provider is wired. Result: the headless CLI was logged in (`claude auth login`, claude.ai) at the start of Stage 3 and all four passed on the first run (`docs/SPIKES.md`). | Stage 2 closed without a logged-in headless CLI (A-20). |
| A-23 | 3 | Architect decision, host model provider: **offline first, key later**. Hosts call Claude through `kintsugi/sandbox/llm_proxy.py` only; until `ANTHROPIC_API_KEY` is added to `.env` the proxy's upstream is a deterministic, clearly labelled offline stand-in per host (`hosts/<h>/evals/offline_model.py`), and baselines recorded with it are labelled `model=offline`. The live Anthropic upstream is built and tested against a fake server; live baselines are re-run when the key arrives. Bedrock is not used yet: `GetFoundationModelAvailability` reports `NOT_AUTHORIZED` for Claude models in account 841535407298 (AWS case 179171381300975). Hard daily cap across all proxy accounts: **$20/day** (ruling D7). | No Anthropic key in `.env`; Bedrock access pending. |
