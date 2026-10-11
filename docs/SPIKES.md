# Spike results (Stages 2–4)

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
