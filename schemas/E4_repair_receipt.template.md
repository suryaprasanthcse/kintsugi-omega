## Kintsugi repair receipt — {INC}
**Symptom:** {symptom_summary}  ·  **Host:** {host}  ·  **Detected:** {t_detect}  ·  **Resolved:** {t_resolve}
**Root cause:** {suspects} — introduced by Entire checkpoint `{ck}` (intent: "{quoted_intent}")
**Fix:** {fix_description}  ·  **Antibody:** {antibody}

| Metric | Before | After | Test |
|---|---|---|---|
| Holdout pass rate | {p_before} ({ci_before}) | {p_after} ({ci_after}) | McNemar exact p = {p} |
| Cost / task | {c_before} | {c_after} | Δ UB {cost_ub} |
| p95 latency | {l_before} | {l_after} | Δ UB {p95_ub} |

**Gates:** G0 ✓ G1 ✓ G2 ✓ G3 ✓ G4 ✓ G5 ✓ G6 ✓ G7 ✓ ({ci_url})
**Traces:** before {trace_links_before} · after {trace_links_after}  ·  **Checkpoints:** {checkpoints}
**Cost of this repair:** ${cost} (budget ${budget}, plan {plan_sha})  ·  **Ledger:** entry {seq}, hash {h}
Fixes #{issue}
