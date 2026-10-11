# Stage verification records

Each stage's verification targets (from `docs/EXECUTION_PLAN.md` §4) with the result and the
evidence that reproduces it. Numbers are copied from committed artifacts or from the named test.

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
