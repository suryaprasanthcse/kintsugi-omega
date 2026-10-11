# Stage verification records

Each stage's verification targets (from `docs/EXECUTION_PLAN.md` §4) with the result and the
evidence that reproduces it. Numbers are copied from committed artifacts or from the named test.

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
- Stage 4 commit 3 (`01M4NRV5K0828Z1TWGGGYGREM4`) has `files_touched: null` in its checkpoint
  (A-37); its trailer, ref and transcript are intact.

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
