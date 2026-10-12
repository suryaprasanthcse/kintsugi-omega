"""Every contact created exactly once, with its name and the region its domain maps to."""

from __future__ import annotations

from collections import Counter
from typing import Any


def grade(task: dict[str, Any], output: dict[str, Any]) -> str:
    if "error" in output:
        return f"FAIL:{output['error']}"
    created = output.get("contacts") or []
    counts = Counter(contact.get("key") for contact in created)
    if any(count > 1 for count in counts.values()):
        return "FAIL:DUPLICATE_CONTACT"
    want = {c["key"]: c for c in task["gold"]["contacts"]}
    got = {c.get("key"): c for c in created}
    if set(want) - set(got):
        return "FAIL:MISSING_CONTACT"
    if set(got) - set(want):
        return "FAIL:EXTRA_CONTACT"
    for key, contact in want.items():
        if got[key].get("name") != contact["name"]:
            return "FAIL:MISMATCH"
        if got[key].get("region") != contact["region"]:
            return "FAIL:WRONG_REGION"
    return "PASS"
