# Stage 2 spike results

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

## Model tier: waiting on a logged-in headless CLI

The standalone `claude` CLI that Surgeons run is not logged in (`claude auth status`:
`"loggedIn": false`). The desktop app authenticates its own sessions through the host, and
those credentials are not borrowed for unattended processes. These four tests are written
and skip with that reason until `claude auth login` is run (or `ANTHROPIC_API_KEY` is wired in
Stage 3):

| Test | Proves |
|---|---|
| `test_a1_n2_commit_after_exit_links_and_passes_c2_to_c5` | A commit made **after** `claude -p` exits links to that session's checkpoint; C2–C5 pass, including C5's byte-exact first prompt; `entire why` agrees; stripped nonce and altered prompt fail. |
| `test_n1_headless_honeytoken_survives_and_c6_catches_it` | N1 and C6 on a headless session, not only on this build session. |
| `test_n5_concurrent_sessions_each_link_their_own_checkpoint` | Each of three concurrent commits links to its own session; no nonce crosses over. |
| `test_a14_kill_mid_tool_use_leaves_no_checkpoint` | A session killed during a tool call leaves no checkpoint and a hashed transcript. |
