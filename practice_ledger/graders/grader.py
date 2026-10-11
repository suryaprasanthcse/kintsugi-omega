"""Every line booked exactly once, at the right USD amount, account and date."""

from __future__ import annotations

from collections import Counter
from typing import Any


def grade(task: dict[str, Any], output: dict[str, Any]) -> str:
    if "error" in output:
        return f"FAIL:{output['error']}"
    booked = output.get("lines") or []
    counts = Counter(line.get("key") for line in booked)
    if any(count > 1 for count in counts.values()):
        return "FAIL:DUPLICATE_ENTRY"
    want = {line["key"]: line for line in task["gold"]["lines"]}
    got = {line.get("key"): line for line in booked}
    if set(want) - set(got):
        return "FAIL:MISSING_ENTRY"
    if set(got) - set(want):
        return "FAIL:EXTRA_ENTRY"
    for key, line in want.items():
        other = got[key]
        if int(other.get("amount_cents", -1)) != line["amount_cents"]:
            return "FAIL:WRONG_AMOUNT"
        if other.get("account") != line["account"] or other.get("date") != line["date"]:
            return "FAIL:MISMATCH"
    return "PASS"
