# Stage verification records

Each stage's verification targets (from `docs/EXECUTION_PLAN.md` §4) with the result and the
evidence that reproduces it. Numbers are copied from committed artifacts or from the named test.

## Stage 6 — Crew, primary incident loop, gates G0–G7, ship (L0, L1, L4)

Recorded 2026-10-12 (IST) on Windows 11 with Entire 0.11.4, entire-graph v0.4.0, the headless
Claude Code CLI 2.1.269, temporalio 1.34.0 (time-skipping server in tests, Temporal CLI 1.9.1 dev
server in the rehearsals) and GitHub Actions. Spikes: `docs/SPIKES.md` (Stage 6); deviations:
amendments A-47 to A-58. Crew models: Sentinel `claude-haiku-5-5`, Surgeons
`claude-sonnet-5-5`, the Opus roles on `claude-opus-5` (A-54).

### The Ledger rehearsal (disposable, disclosed practice target, A-57)

`kintsugi crew rehearse` copies the Ledger practice host outside every repository, delivers the
practice regression through a headless session (attested, with its own checkpoint), runs live
traffic and sends the failures as R-007 hits through the FleetSupervisor on the dev server. Every
attempt is recorded, including the ones that found bugs in Omega:

| Run | What happened | Outcome |
|---|---|---|
| `reh-20261011-224740` | The delivery model refused the patch: its framing promised "keep behavior identical" (A-52). | no incident |
| `…-224903`, `…-225031` | The ruleset's glob blocked the push creating the run's base (N9: a required check also blocks creation). | no incident |
| `…-225239` | Opus calls refused by the CLI (A-54), deterministic fallbacks answered; round 0: three passes, a pre-registered dispute picked H1; G7 in Actions failed its fresh-seed G2 (2/3: the stress profile exhausted a correct retry budget). PR #2 closed with the reason; G2's criterion fixed (A-53). | ESCALATED, nothing merged |
| `…-232114` | Real crew. **Round 0:** H1 (hoist), H2 (revert), H3 (idempotent pass) all passed G0–G4 and G6, but G5 raised intent concerns on all three → no winner, **rewound** (attempt refs kept). **Round 1:** H4 (refined hoist) kept every clause; H5 drew a concern; H6 failed G1. H4 won. PR #3: G7 **passed** in Actions, but the shipper read a skipped job of the same name as not passed. | ESCALATED (I1 held: no merge without a readable G7); fixed in `7d1ec66` |
| `…-235459` | **Resolved end to end** (below). | RESOLVED |

`reh-20261011-235459` (`evidence/rehearsals/reh-20261011-235459.json`), 554 s, $0.65 metered:

| Step | Result |
|---|---|
| Detection | 35 failed live tasks → 35 R-007 hits; the Sentinel paged at 3 in 10 minutes: 1 opened, 34 attached (I7: every hit disposed) |
| Pathologist | one cluster, φ `sha256:b9fe39c5…`, "Duplicate books:/lines write after fx_rate lookup"; **G0: the cassette fails 3/3** at seed 1 on the base |
| Archivist | intent record from checkpoint `01M4PP8XBXDA2H5E0472GM1RYP` (the delivery): 5 contract clauses, **every quote verified verbatim** against the transcript (SHA-256 recorded); the commit trailer and `entire why` **agree**; regression confirmed, **first bad checkpoint** = the delivery's |
| Plan | B = $17.93 (Cap; VaR higher), K = 3, n = 14 pairs needed (120 available), plan `sha256:b34e37c9…` |
| Surgeons | three headless Claude Code sessions in parallel worktrees, **each an attested commit with its own checkpoint** (`01M4PPD46B…`, `01M4PPD6W7…`, `01M4PPDS7S…`); **C2–C5 pass on all three** and on the delivery |
| Auditor | all three pass G0–G4 and G6 (G4: b = 39, c = 0, p = 1.8×10⁻¹²; cost Δ −17%; p95 Δ UB −32%); G5: H1 concern (Opus), H2 and H3 kept every clause (deterministic fallback: the two Opus calls hit the subscription's session limit, recorded in `calls.jsonl`) |
| Dispute | H2 and H3 on the Pareto front → protocol registered (`sha256:66fa65ca…`) before running: stress profile, the same tasks and seeds: 11/12 vs 11/12, cost equal → **smaller diff: H2** (10 vs 18 lines). H2 keeps the refactor's name and docstring and moves the session back outside the retried pass. |
| Ship | issue #4 → **PR #5** whose body is the E.4 receipt (`Fixes #4`) → **G7 green** in Actions (run 38186481441: G1 ✓; G2 no `DuplicateEntry` on the head at 3 fresh seeds, 4 on the base; G3 0 flips; G4 b = 28, c = 0, p = 3.7×10⁻⁹) → merged with merge commit `76f19d1` (second parent: the attested commit) |
| Canary | live batches on the merged code; SPRT accepted H0 → RESOLVED; antibody placeholder recorded for Stage 7 |
| I5 | holdout canary hits: case file 0, crew calls 0, Surgeon transcripts 0 (3 scanned) |
| Ledger | 8 hash-chained entries (transitions, incident, antibody), verifies |

### Targets

| Target | Result | Evidence |
|---|---|---|
| G0 fails 3/3 | ✅ live and in tests (a healthy base does not reproduce) | `test_g0_reproduces_the_regression_and_g2_accepts_only_the_fix`; rehearsal |
| Intent Record with verified quotes | ✅ live (5 clauses, trailer and `entire why` agree); invented quotes are dropped | `test_the_archivist_writes_a_verified_intent_record` (contract), `test_editing_turns_quotes_and_heuristics` |
| Three parallel Surgeons, each with a checkpoint | ✅ live, C2–C5 pass on every attempt | rehearsal evidence `hypotheses[].c2_c5` |
| Round 1 fails and is rewound → round 2 | ✅ live (`…-232114`: three G5 concerns, rewound, round 1 winner) and in the deterministic loop test (naive hoist flips G3, lookup breaks tests, revert ranked down) | `evidence/rehearsals/reh-20261011-232114.json`; `test_round_one_fails_round_two_disputes_then_ships` |
| Pre-registered dispute experiment | ✅ live (twice) and in tests; a protocol changed after registration is refused | `test_the_decision_rule_and_a_pre_registered_dispute` |
| Ship → G7 green → merge | ✅ PR #5, run 38186481441, merge `76f19d1`; locally `LocalShipper` runs G7 in process | `tests/control/test_ship.py` |
| Protected-path edits blocked twice | ✅ the pre-commit hook refuses them on `kintsugi/*` branches; the Auditor's diff check fails a commit that bypassed it; `orchestrator_commit` refuses them before committing | `test_the_precommit_hook_refuses_protected_paths_on_surgeon_branches`, `test_the_auditor_is_blind_and_strict`, `test_the_surgeon_runner_commits_an_attested_change` |
| Diffs over 120 lines blocked | ✅ unless the card records the Coordinator's approval | `test_antigaming_blocks_protected_paths_big_diffs_and_network`, `test_the_plan_and_the_cards` |
| New outbound network calls blocked | ✅ HTTP clients, sockets and URL literals in added lines | same antigaming test |
| Zero holdout leaks across crew spans and transcripts | ✅ live scan above; the loop test scans the case file and every crew prompt; G4 refuses an output carrying the canary or a task id | `test_round_one_fails_round_two_disputes_then_ships`, `test_g4_accepts_the_hoist_and_rejects_the_costly_lookup` |
| The Auditor never sees Surgeon output | ✅ enforced by the case file (refused reads are logged); live: the Auditor's reads are card, repro, cassette, intent record, cause report only | `test_the_auditor_cannot_read_surgeon_output`, rehearsal `information_flow` |
| G6 stops a round when the budget is spent | ✅ no Surgeon session starts past B; the hypothesis fails G6 and `budget_exhausted` ends the rounds | `test_no_surgeon_session_starts_past_the_budget` |
| L4 drill: drift mid-round → rebase → reverify record | ✅ main moves while the Surgeon works; the winner is re-verified merged onto the new main (never rebased, A-49) with the policy record `{G1, G2, G3, G4, G7}` | `test_l4_drift_mid_round_reverifies_the_winner_on_the_new_main` |

### Invariants and proof

- **I1**: nothing merges without G0–G6 and a readable green G7; run `…-232114` shows the
  refusal path (escalated, PR left unmerged).
- **I3**: every live run ended RESOLVED or ESCALATED with a legal transition log; a refused or
  over-budget Surgeon fails its hypothesis, never the incident.
- **I5**: the holdout is decrypted only inside G4 (Auditor) and G7 (Actions secret); outputs are
  aggregates; leak scans are zero.
- **L0, L1, L4**: the primary response, rewind with attempt refs kept, and drift re-verification.
- **C2–C5** pass on every attested commit; **C7**'s artifact (a green G7 run per merged PR)
  exists; **T2** (holdout, scoped key, fresh-seed G7) and **T5** (deterministic gates, blinded
  Auditor, Actions) are exercised.

### Known limits carried forward

- The Opus roles run on `claude-opus-5` until the headless CLI is updated (A-54); two G5
  judgements of the resolved run used the deterministic fallback after the subscription's
  session limit.
- The live rehearsals' round-1 failure came from G5 concerns; the gate-driven round failures
  (G3 flip, failing tests) are shown by the deterministic loop test with scripted Surgeons.
- The Fixes MCP prior (A4) is not connected (OAuth); antibodies, motif scans, vaccination,
  quarantine and the revert path are Stage 7 (the crew answers that none exist yet).
- The ledger is a file until Stage 8 moves it to the `ledger` branch; merge commits made by
  GitHub are committed by GitHub, so Stage 8's C2–C5 must verify the PR record for them.
- Stage 6 commit `fd102e6`'s checkpoint omits one test file from `files_touched` (A-58).

## Stage 5 — Control plane: Temporal + SQLite-WAL + Watchdog (D6)

Recorded 2026-10-11 (IST) on Windows 11 with temporalio 1.34.0, the SDK's time-skipping test
server and the Temporal CLI 1.9.1 dev server (Server 1.32.0). Spikes and drills:
`docs/SPIKES.md` (Stage 5); deviations: amendments A-40 to A-46.

### Targets

| Target | Result | Evidence |
|---|---|---|
| Time-skipping tests for every workflow | ✅ all 8 workflows (FleetSupervisor, Incident, Canary, CrossHostMaturation, VaccinationWave, Onboarding, SabotageCampaign, AuditAnchor) on the time-skipping server; durable timers are skipped, not slept (a canary interval, five hours of anchors) | `tests/control/test_workflows.py` (27 tests, about 21 s) |
| Replay-determinism tests | ✅ recorded histories of a resolved incident, a canary re-entry and an exhausted repair replay without error; an unpatched reordering (reproduce before triage) is caught by replay | `test_recorded_histories_replay_deterministically`, `test_an_unpatched_code_change_is_caught_by_replay` |
| `kill -9` drill, Temporal mode | ✅ live dev server and worker processes: after the worker's process tree is killed, the server records attempt 2 starting after a **HEARTBEAT** timeout; attempt 2 adopts attempt 1's commit by its nonce, or cleans up and commits under a new nonce; through the Watchdog, the chaos API kill → restart → adoption, with fresh pollers on every queue afterwards | `tests/contract/test_control_drill.py` (3 live tests, about 54 s) |
| `kill -9` drill, SQLite mode | ✅ the executor (with its session) and the single writer killed together; on restart the WAL-recovered log folds to REPAIRING, the dead lease is abandoned, attempt 2 adopts or cleans up | `test_reduced_mode_survives_kill9_of_the_whole_stack` (adopt, cleanup) |
| `continue_as_new` | ✅ FleetSupervisor continues every N events (7 events at 3 per run → 3 runs) carrying open incidents, waiting alerts, budgets and recent hit keys (a repeated key is still dropped); AuditAnchor continues daily | `test_the_supervisor_continues_as_new_and_keeps_its_state`, `test_the_audit_anchor_ticks_hourly_and_continues_as_new` |
| All signals and queries | ✅ `hit`, `drift`, `budget_update`, `pause_antibody`, `operator_pause`/`operator_resume`, `incident_closed`; Incident `drift_signal`, `attach`; VaccinationWave `pause`/`resume`; SabotageCampaign `request`/`close`; queries `status`, `open_incidents`, `budget`, `current_state`, `transition_log`, `progress`, `commitments` | supervisor, vaccination and sabotage tests |
| Activities with timeouts, heartbeats, retries and idempotency keys | ✅ the §5.3.2 table row for row (asserted), non-retryable `InvalidAlert`, `BudgetExceeded`, `ProtectedPathViolation`; heartbeats while the crew works; a replayed step returns its recorded result without redoing it; separate crew, surgeon and senses pools | `tests/control/test_activities.py` |
| **I3** (every incident terminates) | ✅ ten terminal scenarios (PARKED, every ESCALATED exit, RESOLVED, the secondary response) each fold to a legal `states.py` log; rounds and canary re-entries are bounded | `test_every_path_ends_terminal_with_a_legal_log`, `test_canary_re_entries_are_bounded` |
| **I1** ordering | ✅ ship runs only after the round's gates, once; a Coordinator naming a hypothesis that failed G0–G6 fails the workflow instead of shipping | `test_a_repaired_incident_resolves_through_every_stage`, `test_a_coordinator_cannot_ship_a_hypothesis_that_failed_its_gates` |
| L4 signal path | ✅ a `drift` signal mid-round triggers `rebase_and_rerun` before the next round | `test_a_drift_signal_mid_round_rebases_and_reruns_the_impacted_gates` |
| T8 (crash recovery not faked) | ✅ the server's own event history is the evidence (attempt 2 after a heartbeat timeout); `kintsugi control history` exports any workflow's history as JSON | `retried_after_heartbeat_timeout`; `tests/control/test_service.py` |
| Watchdog | ✅ restarts a pool at most 5 times in 10 minutes, then pages once; chaos API (bearer token, sabotage id required, Watchdog never a target, ingress outage with automatic recovery); health checks page once per failure; dead-man heartbeat | `tests/control/test_watchdog.py` |
| Senses → control plane | ✅ the queue forwarder turns claimed hits into `FleetSupervisor.hit` signals at least once (failed hand-offs are released and retried) and wakes the Watchdog on `ingestion_silent` for `kintsugi:*` | `test_forwarded_hits_reach_the_fleet_supervisor` |
| Spike N7 | ✅ `entire session resume` restores the session log; `claude -p --resume` continues the same session with its context | `docs/SPIKES.md` |

### Invariants and proof

- **I3**: the control plane enforces `states.py` at runtime: an illegal transition fails the
  workflow before it is recorded, in both Temporal and reduced mode.
- **I1** ordering: the Incident workflow ships only a winner whose G0–G6 verdict passed.
- **L2** (S-18): both modes recover from a `kill -9` with the §6.6 adoption rule; **T8**: the
  evidence is the server's event history.

### Known limits carried forward

- The crew is a port: Stage 6 binds the real Sentinel, Pathologist, Archivist, Coordinator,
  Surgeons and Auditor; Stage 5 runs the loop with scripted crews and the drill Surgeon.
- The Temporal dev server keeps history in memory; the 24-hour run needs the dev server's
  `--db-filename` (or Postgres), set up in WSL2 at Stage 13 (D2).
- The dead-man Lambda and the neatlogs `ingestion_health` rule are Stage 13 / A-38.

## Stage 4 — Senses: neatlogs, receptors, ingress, poller, Fixes

Recorded 2026-10-11 (IST) against the live neatlogs Public API (project tier `free`) with a
service-account token, neatlogs SDK 1.4.26, FastAPI 0.143.0 / uvicorn 0.54.0 and a cloudflared
2026.10.0 quick tunnel to the local ingress. Spike details: `docs/SPIKES.md` (Stage 4);
deviations: amendments A-28 to A-39.

### Targets

| Target | Result | Evidence |
|---|---|---|
| Contract tests against the pinned OpenAPI | ✅ 50 operations pinned from the published document (SHA-256 recorded); all 17 recorded live responses validate, mutated ones fail; live: the published document has **no drift** from the pin and live responses validate (4/4 contract tests) | `tests/senses/test_neatlogs_api.py`; `KINTSUGI_CONTRACT=1 … tests/contract/test_neatlogs_contract.py` |
| The Stage 3 `grader.verdict` spans are readable through the API (first contract test) | ✅ readable (`"PASS"`, JSON-encoded); **found** each verdict was its own one-span trace and fixed it, so a task's verdict and tool spans share `task:<id>` (A-28), verified live | `test_grader_verdicts_are_readable_through_the_api` |
| Duplicate events are dropped with 200 | ✅ a repeated event id answers 200 `duplicate`, enqueues and persists nothing; the dedupe holds 24 h | `tests/senses/test_ingress.py`, `test_event_dedupe_holds_for_24_hours_then_expires` |
| Ingress answers within 2 s | ✅ 200 signed deliveries: median 4.5 ms, p99 21.2 ms, max 68.5 ms (in-process); reachable through the tunnel (`/healthz`) | `test_ingress_answers_within_two_seconds` |
| Webhook verification | ✅ HMAC-SHA256 signature in the format neatlogs actually sends (spike A6) over the real delivery body, 256-bit secret path (404 otherwise), optional source allowlist, size cap, malformed bodies refused | `test_signature_secret_path_source_and_shape_are_enforced` |
| The poller recovers hits during a simulated outage | ✅ ingress at 503 while three hits arrive → the poller claims all three; after recovery the redelivered events claim nothing; restart from the persisted watermark recovers hits missed while down; an expired cursor restarts the sweep without double claims; concurrent claimers never double-process (150 hits × 4 threads) | `tests/senses/test_poller.py` |
| Replayed deliveries are suppressed | ✅ by event id (200 `duplicate`) and by hit key (a new event for an already-claimed hit claims nothing) | `test_a_replayed_delivery_is_suppressed_by_event_id_and_by_hit_key` |
| I7 completeness function on fixtures | ✅ passes when every hit in W opened or attached to an incident; fails on a missing or undecided hit; ignores hits outside W | `tests/senses/test_receptors.py` |
| I7 on live data | ✅ after one sweep: **281 hits, 0 missing**; all pending without a Sentinel (correctly FAIL); with stand-in dispositions in the local store: `281 hits -> 3 incidents + 278 dedupes (0 pending, 0 missing) PASS` | `kintsugi senses completeness --since 2026-10-11T15:30:00Z` |
| R-007 fires on Ledger `FAIL:` verdicts in a rehearsal (no sabotage committed) | ✅ three rehearsals (seeds 11, 12, 13) under the disclosed FX-outage profile: 18/18 tasks `FAIL:FX_UNAVAILABLE`; **every failing trace that reached neatlogs got an R-007 hit within seconds (15/15)**; the poller claimed them. One trace per run never appeared (A-39). R-001 Ledger also hit the FX 503 errors (10 new hits in the sweep after the third run) | `kintsugi senses rehearse`; `kintsugi/sandbox/profiles/rehearsal-fx-outage.yaml` |
| Receptors R-001…R-007 per host | ✅ provisioned live: 12 neatlogs detections (R-001, R-005, R-006, R-007 × 3 hosts) with per-event rules to the ingress webhook, and 9 Omega metric receptors (R-002/3/4 × 3); re-provisioning is idempotent and retires superseded versions and rules | `kintsugi senses provision`; `test_provisioning_is_idempotent_and_retires_stale_versions` |
| No false positives on the healthy fleet | ✅ R-002/R-003/R-004: 0 hits over 1,387 Ledger, 345 Scout and 576 Triage traces; R-006 v1 hit 137 healthy Ledger answers (`PHONE_NUMBER`), so v2 drops that entity for Ledger | `kintsugi senses poll --once`; spike per entity |
| Tag writes with optimistic concurrency | ✅ read-modify-write with `expectedRevision`, re-read on 409, no-op merges write nothing; live revision 0 → 1 | `tests/senses/test_records_and_tags.py` |
| Spikes A3, A5, A6, A7, A11, A13 (+ A12 probe) | ✅ recorded; A5 sharing cannot be enabled by API; A7 custom attributes are not readable; A13 rules can be created | `docs/SPIKES.md` |

### Invariants and proof

- **I7** (completeness): one shared `seen_hits` table claims each hit once across the webhook and
  the poller; the completeness function is the C9 primitive. The poller is the source of truth.
- **C8** primitive: `NeatlogsApi.trace(id)` reads a trace's existence and timestamps.
- **L0 detection** (R-007 on Ledger failures) and **L2** (poller recovery, replay suppression,
  the ingress's 503 outage switch) are exercised above.

### Known limits carried forward

- **Alerting (A-38):** after the first per-event delivery no alert rule in the project fired
  again; webhooks are therefore unproven beyond that delivery. The Architect is asked to check
  the neatlogs alerting UI or support. Omega pages from its own store (the Sentinel applies the
  per-host thresholds), so detection does not depend on it.
- **Ingestion loss (A-39):** about 3–4% of task traces never become visible; I7 can only cover
  what neatlogs reports. Investigation carries into Stage 5 and Stage 13.
- **OAuth-only operations (A-29):** delivery replay, webhook test, detection preview, generate
  and backfills need `NEATLOGS_OAUTH_TOKEN` (not configured); preview and backfills are also
  gated off in production.
- The webhook points at a cloudflared **quick tunnel** that ends with this session; rerun
  `kintsugi senses provision --public-url <origin>` against a stable origin before T0.
- R-005 is a `refusals` stand-in; conditional detections never fired, so R-002/3/4 run in Omega.
- The Fixes MCP is an interface with an unconfigured implementation (spike A4 is Stage 6).
- Two Stage 4 checkpoints under-report `files_touched` (A-37): commit 3
  (`01M4NRV5K0828Z1TWGGGYGREM4`, null) and commit 4 (`01M4NVAAQKHEQ58DCBE9HK0S46`, only
  `pyproject.toml`). Their trailers, refs and transcripts are intact; the cause was an edited
  file left uncommitted across commits of one turn.

## Stage 3 — Fleet: Ledger, Scout, Triage + sandbox and proxies

Recorded 2026-10-11 (IST) on Windows 11 + WSL2 Ubuntu 26.04, Docker Engine 29, gVisor
release-20261005.0, neatlogs SDK 1.4.26, anthropic SDK 1.13.0.

**Model mode: offline (amendment A-23).** No Anthropic key is configured yet, so every host
called its labelled offline stand-in through the LLM metering proxy. These baselines exercise
the whole host (tools, retries, chaos, sandbox-ready proxies, grading, telemetry); they are not
a measurement of Claude. Live baselines are re-run with `kintsugi fleet baseline` once
`ANTHROPIC_API_KEY` is in `.env`.

### Baselines (background chaos, seeds 1–3, train + sealed holdout)

| Host | SLO | Passed | Pass rate | Wilson 95% | p95 latency | Chaos absorbed | Health card |
|---|---|---|---|---|---|---|---|
| Ledger | ≥ 90%, p95 ≤ 25 s | 360 / 360 | 1.000 | [0.989, 1.000] | 0.42 s | 102 `fx_rate` 429s in 1,149 calls | `evidence/baselines/ledger.json` |
| Scout | ≥ 85% | 180 / 180 | 1.000 | [0.979, 1.000] | 0.55 s | 77 `fetch_document` 429s in 1,986 calls | `evidence/baselines/scout.json` |
| Triage | ≥ 88% | 282 / 300 | 0.940 | [0.907, 0.962] | 0.33 s | — (no GitHub faults in the background profile) | `evidence/baselines/triage.json` |

Per seed: Ledger 120/120 ×3, Scout 60/60 ×3, Triage 94/100 ×3. Triage's 18 failures are the
Haiku stand-in's declared blind spot (A-27): issues with Japanese titles (12 `WRONG_COMPONENT`,
6 `WRONG_TYPE`). Metered cost (list price, non-billable): Ledger $0.0428, Scout $0.0120, Triage
$0.0266.

### Targets

| Target | Result | Evidence |
|---|---|---|
| Baselines at 3 seeds ≥ SLO with Wilson CIs | ✅ all three hosts (offline model) | table above; `kintsugi fleet baseline` |
| Ledger handles the 8% background 429s with **no duplicate journal lines** | ✅ 102 injected 429s, zero `FAIL:DUPLICATE_ENTRY` across 360 tasks | `evidence/baselines/ledger.json` |
| Ledger keeps the S-17 target shape | ✅ live graph: `run_reconcile` calls `retry_with_backoff` and contains `attempt`; `ReconcileSession` (mutable accumulator) is constructed by `run_reconcile`, not `attempt`; `ReconcileSession.post` reaches the side-effecting `post_journal_entry`. Adding only `attempt -CONSTRUCTS-> ReconcileSession` and the call it types completes motif v1.1 | `tests/contract/test_fleet_motifs.py` |
| The healthy fleet has no motif false positive; Scout is the disclosed look-alike | ✅ v1.1 matches no host; v1.0 matches exactly Scout (`fetch_all_pages` / `attempt` / `ScratchBuffer`) | `tests/contract/test_fleet_motifs.py` |
| Deterministic cassette replay | ✅ Ledger, Scout and Triage: replayed verdicts equal the recorded ones on 3/3 replays; the cassette carries model answers and keeps injected 429s at their positions; an unseen request fails the task instead of calling out | `tests/fleet/test_replay.py` |
| Chaos proxy identical per seed | ✅ same faults at the same (task, occurrence) under three different thread interleavings; a different seed differs; an injected status never reaches a side-effecting upstream | `tests/sandbox/test_chaos_proxy.py` |
| LLM proxy enforces per-host budgets and the daily cap | ✅ worst-case reservation in one SQLite transaction: 60 concurrent $0.50 reservations against a $20 cap grant exactly 40; refusals are HTTP 402 and nothing is forwarded; the cap resets per UTC day; offline traffic is metered but never consumes the cap | `tests/sandbox/test_llm_proxy.py` |
| LLM proxy meters cost within 1% | ✅ metered = list-price cost of the reported usage (cache writes 1.25×/2×, cache reads, Haiku 5.5 long-context tier), including streamed `message_delta` usage; the real Anthropic SDK works through it (create and stream) | `tests/sandbox/test_llm_proxy.py`, `tests/econ/test_prices.py` |
| Sandbox blocks non-allowlisted egress (spike N8) | ✅ under `runsc` as uid 65534 with a read-only root, only the gateway's allowlisted route answers; direct IPs, DNS, the bridge and unlisted routes fail; the wall-clock limit kills the container | `tests/contract/test_sandbox_contract.py`; `docs/SPIKES.md` |
| `neatlogs doctor --local --json` and `--probe --json` pass | ✅ local: `local_envelope`; probe: visibility, finalization, hierarchy, attributes, input/output, metadata, typed tokens | run 2026-10-11 with the `.env` key passed to the child only |
| `grader.verdict` spans | ✅ every graded task emits `grader.verdict` with `PASS` / `FAIL:<reason>` (`hostkit.telemetry`); baselines exported with the SDK key. Reading them back through the Public API is the Stage 4 client's first contract test | `hosts/hostkit/hostkit/telemetry.py` |
| Holdout unreadable without the key; canary strings; manifest matches | ✅ age-encrypted to `.kintsugi/holdout.pub`; the canary is in every holdout task and absent from the ciphertext; a wrong identity fails; `kintsugi holdout verify` checks `.kintsugi/holdout.sha256` and catches a one-byte change | `tests/fleet/test_fleet.py`, `tests/test_cli.py` |
| Carried model-tier spikes (A-22) | ✅ A1/N2, N1 headless, N5 linkage, A14 mid-tool kill: 4 passed | `docs/SPIKES.md` |
| Fixture-repo motif tests keep passing | ✅ unchanged | `tests/memory/test_graph.py`, `tests/contract/test_graph_contract.py` |

### Invariants touched

- **I4** (spend within budget): the proxy refuses any call whose worst case would breach the
  $20/day cap or the host's monthly LLM budget, atomically across threads and processes.
- **I5** (holdout never in crew context): holdouts are generated from a secret seed, encrypted,
  canaried and decrypted only by Omega to be piped into a host process on stdin.

### Known limits carried forward

- All baselines are `model=offline` (A-23). Live baselines need `ANTHROPIC_API_KEY`; Bedrock is
  `NOT_AUTHORIZED` for Claude in the account (AWS case 179171381300975).
- Holdouts are sealed to a rehearsal identity; the T0 checklist re-seals them under Architect
  custody (A-26).
- `g7-gates.yml` is the Stage 3 skeleton (G1, G3, manifest, G4 fresh-seed run); G2 and G4's
  paired statistics arrive with incidents in Stage 6.
- The runtime (`kintsugi fleet up`) is tested in accelerated runs; the 24-hour run starts from
  WSL2 at T0, never from inside the desktop app (D2).
- Scratch observations, deliberately not committed so the sabotage mechanism is not seeded into
  the repository: Ledger with the session built inside `attempt` fell to 82–88% with
  `FAIL:DUPLICATE_ENTRY` under the same chaos; Scout with the buffer hoisted out of `attempt`
  failed 19/200 tasks with `FAIL:DUPLICATE_CITATION` while the intended code passed 200/200.
