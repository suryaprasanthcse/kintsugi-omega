"""Ledger's task entry point: one vendor document in, its journal postings out."""

from __future__ import annotations

from typing import Any

from neatlogs import span

from ledger.db import LedgerDB
from ledger.reconcile import build_batch, process_invoice
from ledger.tools import lookup_vendor, parse_invoice, reconcile_report


@span(kind="WORKFLOW", name="reconcile_invoice")
def run_task(task: dict[str, Any], db: LedgerDB | None = None) -> dict[str, Any]:
    """Read the document, find the vendor, post the journal lines, report."""
    db = db if db is not None else LedgerDB()
    parsed = parse_invoice(task["document"])
    vendor = lookup_vendor(parsed.vendor_name)
    batch = build_batch(parsed, vendor)
    process_invoice(db, batch)
    return {
        "ref": batch.ref,
        "lines": [line.as_dict() for line in db.lines(batch.ref)],
        "report": reconcile_report(db, batch.ref),
    }
