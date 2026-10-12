"""The CRM practice host's task entry point: one contact batch in, the CRM's contacts out."""

from __future__ import annotations

from typing import Any

from neatlogs import span

from practice_crm.sync import build_batch, sync_contacts
from practice_crm.tools import list_contacts


@span(kind="WORKFLOW", name="sync_batch")
def run_task(task: dict[str, Any]) -> dict[str, Any]:
    batch = build_batch(task["batch"])
    sync_contacts(batch)
    return {"ref": batch.ref, "contacts": list_contacts(batch.ref)}
