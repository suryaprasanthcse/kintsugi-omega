"""The practice ledger's task entry point: one document in, its booked lines out."""

from __future__ import annotations

from typing import Any

from neatlogs import span

from practice_ledger.reconcile import build_batch, reconcile
from practice_ledger.tools import list_lines


@span(kind="WORKFLOW", name="book_document")
def run_task(task: dict[str, Any]) -> dict[str, Any]:
    batch = build_batch(task["document"])
    reconcile(batch)
    return {"ref": batch.ref, "lines": list_lines(batch.ref)}
