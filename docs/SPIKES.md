# Spike results (Stages 2–6)

Spikes run against the installed Entire 0.11.4, entire-graph v0.4.0 and Claude Code 2.1.269 on
Windows 11, 2026-10-11. Each result is locked by a test: CI runs the golden-fixture tests;
`KINTSUGI_CONTRACT=1 uv run pytest tests/contract -v` re-runs the live ones.

| Spike | Question | Result | Evidence |
|---|---|---|---|
| A9 | entire-graph snapshot record schema | ✅ **Pinned.** Schema 1.1: a header record, then `file`, `external`, `symbol`, `relation`, `summary`. A nested closure is its own `function` symbol, reached from its enclosing function by `CONTAINS`. The enclosing function is **also** credited with the closure's calls and constructions. | `tests/fixtures/graph/snapshots/*.ndjson`; `tests/contract/test_graph_contract.py` (live = golden) |
| A9 → motif | Does the reconciled motif hold on real output? | ✅ v1.0 binds `R=retry_with_backoff, Enc=run_reconcile, F=attempt, S=ReconcileSession` on `ledger_before`, does not match `ledger_after`, over-matches Scout. v1.1 (`[F,T,CALLS*]`, side-effecting `T`) excludes Scout. The mutable-accumulator scan runs on real source. | `tests/memory/test_graph.py` |
| N1 | Do passphrase nonces and honeytokens survive Entire's redaction? | ✅ **Yes, byte-exact**, on Entire's real redaction pipeline (checkpoint `01M4N6NY5RRR1AN0HX900T2DV4`). A base64 high-entropy decoy planted alongside them **was redacted** by Entire's built-in scanners, confirming the risk N1 was raised for. A hex decoy survived: built-in detection is inconsistent, so only the passphrase format is safe. | Checkpoint `01M4N6NY…DV4` |
| N1 → rules | Are Kintsugi's custom redaction rules live? | ✅ `redaction.custom_redactions` (a name → RE2 map; list forms are rejected) loaded with `inline_patterns: 8`; the planted `kintsugi-redaction-probe-806693` is absent from the stored transcript. | `.entire/settings.json`; `tests/proof/test_redaction.py` |
| C6 | Does the honeytoken check catch a planted token? | ✅ The positive control found the planted honeytoken in both `0/full.jsonl` and `0/transcript.jsonl` of the real checkpoint. The negative control (a fresh token) found zero hits. | `kintsugi/proof/redaction.py::honeytoken_hits` |
| C4 | Does C4 work on a real checkpoint? | ✅ The nonce clause passes; a stripped-nonce copy fails two ways (content hash and nonce). The `files_touched` clause **flagged a real gap**: a file written by a shell command was not recorded by Entire, so the orchestrator now refuses changes not explained by the session's Write/Edit calls. | Checkpoint `01M4N6NY…DV4`; `tests/proof/test_commit.py` |
| C4 × `why` | Does `entire why` agree with the trailer mapping? | ✅ Sampled added lines of `30ca283` resolve to its own commit and checkpoint. | `kintsugi/proof/commit.py::why_agrees` |
| N4 | Do Entire's hooks fire inside linked worktrees? | ✅ Git hooks: a linked worktree resolves the common hooks directory Entire installed into. Claude Code hooks: a headless session in `wt/INC-S/H1` registers under that worktree path. | `test_n4_hooks_fire_inside_a_linked_worktree` |
| N5 | Do concurrent sessions in three worktrees stay isolated? | ✅ **Registration tier:** three concurrent headless sessions each register under their own orchestrator-chosen ID and their own worktree path. | `test_n5_concurrent_sessions_register_in_isolation` |
| A14 | Abandoned attempt recovery | ✅ **Mechanics:** a session killed early is stopped in Entire (`session stop --force`), the worktree is reset and cleaned, no checkpoint ref appears, and the transcript (when one exists) is hashed. | `test_a14_abandoned_attempt_mechanics` |
| Gateway | Windows process launching | ✅ Two real bugs found and fixed. npm `.cmd` shims are followed to the real `.exe` and batch files are refused (prompt text would reach `cmd.exe`). stdin is sent as exact bytes (text mode rewrote `\n` as `\r\n`). Prompts go in on stdin from a file (Windows caps command lines near 32k characters). | `tests/sandbox/test_proc.py`, `tests/crew/test_claude_session.py` |

## Model tier (carried into Stage 3 by amendment A-22)

Stage 2 closed with the standalone `claude` CLI logged out; the desktop app's host-scoped auth
is never borrowed for unattended processes (A-20). At the start of Stage 3 the headless CLI
(2.1.269) reported `"loggedIn": true` (`authMethod: claude.ai`), and the four model-tier tests
ran against real headless sessions on `haiku`, 2026-10-11 16:20 IST:
`KINTSUGI_CONTRACT=1 uv run pytest tests/contract/test_spikes.py -k "a1_n2 or n1_headless or
each_link_their_own or a14_kill"` → **4 passed in 63.4 s**.

| Spike | Test | Result |
|---|---|---|
| A1/N2 | `test_a1_n2_commit_after_exit_links_and_passes_c2_to_c5` | ✅ A commit made **after** `claude -p` exits links to that session's checkpoint; C2, C3, C4 (nonce verbatim) and C5 (byte-exact first prompt) pass; `entire why` agrees with the trailer; a stripped nonce and a one-byte prompt change both fail. |
| N1 (headless) | `test_n1_headless_honeytoken_survives_and_c6_catches_it` | ✅ The passphrase nonce and honeytoken survive Entire's redaction in a headless session; C6 finds the planted honeytoken and a fresh token scores zero. |
| N5 (linkage) | `test_n5_concurrent_sessions_each_link_their_own_checkpoint` | ✅ Three concurrent sessions in three worktrees each commit and link to their **own** checkpoint; no nonce crosses over. |
| A14 (mid-tool) | `test_a14_kill_mid_tool_use_leaves_no_checkpoint` | ✅ A session killed at its first `tool_use` event leaves no checkpoint ref; the abandoned transcript is hashed. |

Every Stage 2 spike is now resolved at both tiers.

## Stage 3 spikes

| Spike | Question | Result | Evidence |
|---|---|---|---|
| N8 | Does gVisor in WSL2 isolate a host and block non-allowlisted egress? | ✅ **Yes.** Docker Engine 29 (Ubuntu 26.04 archive) and gVisor `runsc` release-20261005.0 (gVisor's signed apt repository) installed in WSL2 Ubuntu, `runsc install` registered the runtime. Under `--runtime runsc` (`platform.release()` = `4.19.0-gvisor`), as uid 65534 with a read-only root, a probe on an `--internal` network reaches an upstream **only** through the egress gateway's allowlisted route (200); an unlisted route is 403 `egress_denied`; a direct connection to 1.1.1.1, external DNS and the upstream's bridge address all fail; `/work` (tmpfs) is writable. The wall-clock limit kills the container itself. **Finding:** Docker's embedded DNS (127.0.0.11, an iptables redirect in the container's namespace) does not answer under `runsc`, so the gateway is pinned with `--add-host`; Docker 29 leaves `.NetworkSettings.IPAddress` empty, so addresses are read per network. | `tests/contract/test_sandbox_contract.py` |

## Stage 4 spikes (neatlogs Public API)

Run 2026-10-11 between 14:20 and 16:00 UTC against `https://app.neatlogs.com/api/v1/public` with a
service-account token, neatlogs SDK 1.4.26, a cloudflared 2026.10.0 quick tunnel and project
tier `free`. The contract is the published OpenAPI 3.1 document (285 operations), pinned as a
50-operation subset (`kintsugi/senses/contract/`). Live responses are golden fixtures in
`tests/fixtures/neatlogs/` (train-split Ledger traces and synthetic spike traces only);
`KINTSUGI_CONTRACT=1 uv run pytest tests/contract/test_neatlogs_contract.py` re-checks them.

| Spike | Question | Result | Evidence |
|---|---|---|---|
| Credential | Does the SDK key reach the Public API? | ❌ **No.** `NEATLOGS_API_KEY` is refused as `x-api-key` and as Bearer (401 `UNAUTHORIZED`); the docs confirm ingest keys never authorize reads. A service-account token (`nlsa_…`, Bearer + `x-project-id`) works. Webhook test and delivery replay, detection preview, generate and backfills accept **only an OAuth user token**; preview, generate, backfills and custom classifiers are also behind the disabled `tier-c-compute` gate. | `test_operations_that_accept_only_oauth_are_known` |
| A3 | Can an incident's traces share a session id? | ✅ `trace(session_id=…)` / `@span(session_id=…)` set it on the root span only. `GET /traces?sessionId=inc-S7dc19d` returned both traces of the session (one immediately, the second within 90 s); `GET /sessions/{id}` reports `traceCount`, cost and tokens. `GET /sessions/{id}/export` answered a retryable 503 once. | `tests/fixtures/neatlogs/session_get.json` |
| A5 | Can trace sharing be enabled for judges? | ❌ Only `GET /traces/{id}/sharing` exists (state `disabled`); no operation enables it. Fallback: `GET /traces/{id}/export` JSON inside proof bundles. | `trace_sharing.json`, `trace_export.json` |
| A6 | Webhook signature and retry policy | ✅ Signature: **`X-Neatlogs-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>`**, verified on a real delivery; the signed material carries no timestamp. Also `X-Neatlogs-Event` and `X-Neatlogs-Delivery-Id`. The body's `id` is the event id (`alert:<uuid>`); `data.detection` carries the detection and span ids, `data.traceId` the trace, but **no hit id**. A per-event alert was delivered under 1 s after the hit. A delivery answered with HTTP 500 was recorded `failed` (attempt 1) and **not retried** within 35 minutes; replay is OAuth-only. | `webhook_delivery_per_event.json`, `webhook_deliveries.json` |
| A7 | Span kinds and custom attributes | ✅ Kinds round-trip: `WORKFLOW`, `AGENT`, `TOOL`, `EVALUATOR`, `CHAIN`, `LLM` (the SDK knows 15). ❌ **Custom attributes** (`trace(**attrs)`, `@span(parameters=…)`) are readable through no endpoint; spans expose input, output and error only, and span-level `tags=` replaced the trace's init tags. Manifests and budgets therefore go into span input/output, and trace tags are written with `PATCH`. A raised tool error is an `ERROR` span with empty output and the message in `error`. | `trace_raw_spans.json`, `span_payload_tool.json` |
| A11 | Rate limits | ✅ Per-class token buckets that refill over 60 s: `source-perimeter` 600 (before auth), `metadata` 120, `standard-read` 300, `standard-write` 60, `search-analytics` 30 (raw spans), `export-import-outbound` **10** (span payloads, exports). Ten payload reads in 6 s emptied the bucket; the next got 429 `RATE_LIMITED` with `Retry-After` 1–6 s. Headers: `RateLimit-*`, `X-NeatLogs-RateLimit-*`. | `test_rate_limit_headers_name_their_class` |
| A12 | Backfill bounds | ⚠️ `POST /detections/{id}/backfills` is OAuth-only and gated. Without it, new detections still scored recent history: right after the first rehearsal, the new receptors hit Stage 3 baseline traces from 3.5 hours earlier (a periodic matcher pass after new traffic). Its look-back was not measured. | live hits on `phase:baseline` traces |
| A13 | Can alert rules be created through the API? | ✅ **Yes**: `POST /alert-rules` (threshold, per-event) with a service token, `cooldownMinutes ≥ 1`; webhooks attach through `channelMask.webhookIds` (one per rule). ⚠️ After the first per-event delivery no rule in the project fired again for about an hour (amendment A-38). | `alert_rule_create.json`, `alert_history.json` |
| Detections | Which detection types work? | ✅ regex (input and output patterns, error text included), PII (per span) and the built-in `refusals` classifier (scores 0.86–0.87) all fire within seconds. ❌ conditional detections accepted `tool_call_count`, `toolCallsCount`, `latency_ms`, `cost_usd`, with or without a workflow filter, and never fired. PII `PHONE_NUMBER` matched healthy Ledger journal lines (3 of 6 fresh answers; 137 baseline answers); the other entities matched none. | `kintsugi/senses/receptors/` |
| Traces | Shape of a host's trace | ⚠️ Each `grader.verdict` was its own one-span trace; fixed so the verdict shares the `task:<id>` trace (A-28), verified live. Healthy tool calls per task: Ledger median 10 (max 27), Scout 11 (27), Triage 3 (5). About 3–4% of task traces never appear (A-39). | `trace_spans.json` |
| Writes | Idempotency and tag revisions | ✅ `PATCH /traces/{id}` with `expectedRevision` moved revision 0 → 1; a UUIDv5 idempotency key was refused (`INVALID_IDEMPOTENCY_KEY`), base64url SHA-256 accepted. | `tests/senses/test_records_and_tags.py` |

## Stage 5 spikes and drills (control plane)

Run 2026-10-11 (IST) on Windows 11 with temporalio 1.34.0. The SDK fetched its two single
binaries on first use (ruling D6): the time-skipping test server (CI and unit tests) and the
Temporal CLI 1.9.1 dev server (Server 1.32.0, in-memory persistence) for the live drills.
`KINTSUGI_CONTRACT=1 uv run pytest tests/contract/test_control_drill.py
tests/contract/test_spikes.py -k "drill or n7"` re-runs them.

| Spike | Question | Result | Evidence |
|---|---|---|---|
| N7 | Can a headless session be resumed in place (L2 orphan recovery, long-lived roles)? | ✅ After the local transcript was removed (a fresh machine), `entire session resume <branch> --force` restored it from the checkpoint; `claude -p --resume <id>` then continued the **same** session id and recalled the nonce written in the first run (haiku, about 20 s, under $0.01). `SessionRequest(resume=True)` emits `--resume` instead of `--session-id`. | `test_n7_session_resume_restores_the_log_and_continues_the_same_session` |
| L2 / Temporal | Does a `kill -9` of the Surgeon pool lead to attempt 2 with the right adoption rule? | ✅ Worker processes on the dev server; the worker's process tree is killed while attempt 1 runs; a new worker starts. The server's history shows attempt 2 starting with a `HEARTBEAT` timeout as its last failure. **Adopt**: attempt 1 had committed with its nonce; attempt 2 keeps that commit and starts no session. **Cleanup**: attempt 1 had only dirtied the worktree; attempt 2 kills the orphan, resets and cleans, and commits under a new nonce. **Through the Watchdog**: the chaos API kills the pool, the supervisor restarts it, attempt 2 adopts, and every task queue's pollers are fresh afterwards. 3 tests, about 54 s. | `tests/contract/test_control_drill.py` |
| L2 / SQLite | Does reduced mode survive a `kill -9` of the whole stack? | ✅ Executor (with its session) and the single writer killed together; on restart the WAL-recovered log still folds to REPAIRING, the dead executor's lease is marked abandoned, and the restarted executor runs the step as attempt 2 (adopt and cleanup cases). | `test_reduced_mode_survives_kill9_of_the_whole_stack` |
| Kill semantics | Does `proc.kill_tree` reach the session under a killed worker? | ⚠️ On Windows the whole tree dies. On Linux the worker and its process group die, but a grandchild that leads its own session (the Surgeon's session) survives as an orphan; attempt 2 kills it through the worktree lock before adopting or resetting, as §6.6 requires. | `kintsugi/control/surgeon_step.py` |

## Stage 6 spikes and findings (crew, gates, ship)

Run 2026-10-12 (IST) on Windows 11 with Entire 0.11.4, entire-graph v0.4.0, the headless
Claude Code CLI 2.1.269 (logged in via claude.ai), temporalio 1.34.0 with the Temporal CLI 1.9.1
dev server, and GitHub Actions. Rehearsal evidence: `evidence/rehearsals/*.json`
(aggregate-only); `uv run kintsugi crew rehearse --github suryaprasanthcse/kintsugi-omega
--holdout-identity <rehearsal identity> --model <opus role>=claude-opus-5 …` re-runs it.

| Spike | Question | Result | Evidence |
|---|---|---|---|
| N9 | Rulesets and merge identity | ✅ A ruleset (`kintsugi-rehearsal-main`, id 24898487) on the rehearsal base branches requires a pull request and the check `g7 / G7 · ledger-practice`, forbids force-pushes and deletion, with **no bypass actor**. A direct push to a protected base was refused (`GH013 … push declined due to repository rule violations`) in every run. PR #5 merged only after G7 passed. The merge commit `76f19d1` is authored by Surya Prasanth R, **committed and signed by GitHub** (`noreply@github.com`, verified), merged by `suryaprasanthcse`; its second parent is the attested Surgeon commit `e50387e` (author = committer = Surya), which therefore survives with its signature and checkpoint. ⚠️ Findings: (1) a required status check also blocks the push that **creates** a matching branch, so the ruleset lists branches explicitly, each added right after its creation; (2) rebase and squash merges rewrite commits, so Omega merges with merge commits and does not require linear history (A-51); (3) a skipped job sharing the required check's name made the check ambiguous, so the reusable workflow's fleet job has its own name. | `kintsugi/crew/rehearsal.py::github_setup`, PR #5, run 38186481441 |
| A4 | neatlogs Fixes MCP (the Coordinator's prior) | ⏸ Not connected: the Fixes tools need an OAuth user token (A-29), which is not configured. The Coordinator runs without the prior (`fixes_prior` → none); the port is wired (`OmegaCrew(fixes=…)`, `triage_list` → `triage_get_investigation`). | `kintsugi/senses/fixes_mcp.py`, `kintsugi/crew/factory.py` |
| G3 tool | Does `entire graph verify` adjudicate pytest? | ✅ With `pytest -rA` (or `-v`) it parses per-test ids and reports `NEWLY FAILING (n): …` and a verdict; ❌ with `-q` it degrades to an exit-code verdict. It always exits 0 (A-48). | `tests/gates/test_stage6_gates.py::test_g3_with_entire_graph_verify` |
| Delivery | Will a model apply a regression framed as "no behaviour change"? | ❌ No: Claude Haiku 5.5 read the practice patch, explained that moving the session into the retried closure re-posts lines after a 429 and contradicts "keep behavior identical", and applied nothing. With the spec's own framing (§6.2 step 3) it applied the patch (A-52). | first rehearsal attempt (no commit, so no checkpoint) |
| Crew models | Which models can the headless CLI run? | ⚠️ CLI 2.1.269 refuses `claude-opus-5-5` ("2.1.280 or newer is required"); `claude-sonnet-5-5` and `claude-haiku-5-5` work; the `opus` alias resolves to `claude-opus-5`. The Opus roles ran on `claude-opus-5` (A-54). | `calls.jsonl` per incident |
| Live loop | Does the whole primary response run with real models? | ✅ Three live rehearsals (details in `docs/VERIFICATION.md`, Stage 6). The third resolved end to end in 9 minutes for $0.65 of metered model spend (notional on the subscription). | `evidence/rehearsals/reh-20261011-235459.json` |
