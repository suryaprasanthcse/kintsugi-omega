# Published bundles

`kintsugi audit --all --offline ./published-bundles` (and `make judge`) read this directory: the
window record, the ledger export, anchors, sabotage reveals and one `<INC>/bundle.json` per
incident of the autonomy window W (spec §7.4, amendment A-67).

W was not run (Architect's go/no-go, 2026-10-12), so no bundle is published. The offline audit
therefore reports the window-bound checks as SKIP (exit 2) and passes what needs no window: C11
(the AB-1 lineage replays) and C14. Each check's behaviour on clean and tampered inputs is shown
by `uv run pytest tests/proof/test_audit.py -q`.
