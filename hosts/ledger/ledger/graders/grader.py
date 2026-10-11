"""Exact journal-line match (spec §5.9.1): account, amount to the cent, currency and date.

A line posted twice is ``FAIL:DUPLICATE_ENTRY`` (the S-17 symptom); a missing line is
``FAIL:MISSING_ENTRY``; anything else that differs names the first differing field.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

Key = tuple[str, int, str, str]


def _key(line: dict[str, Any]) -> Key:
    return (str(line["account"]), int(line["amount_cents"]), str(line["currency"]),
            str(line["date"]))  # fmt: skip


def grade(task: dict[str, Any], output: dict[str, Any]) -> str:
    if "error" in output:
        return f"FAIL:{output['error']}"
    posted = output.get("lines") or []
    keys = Counter(line.get("key") for line in posted)
    if any(count > 1 for count in keys.values()):
        return "FAIL:DUPLICATE_ENTRY"
    want = Counter(_key(line) for line in task["gold"]["lines"])
    got = Counter(_key(line) for line in posted)
    if got == want:
        return "PASS"
    missing, extra = want - got, got - want
    if not extra:
        return "FAIL:MISSING_ENTRY"
    if not missing:
        return "FAIL:DUPLICATE_ENTRY" if all(k in want for k in extra) else "FAIL:EXTRA_ENTRY"
    (_, m_cents, _, m_date), (_, e_cents, _, e_date) = min(missing), min(extra)
    if m_date != e_date:
        return "FAIL:WRONG_DATE"
    if {k[0] for k in missing} != {k[0] for k in extra}:
        return "FAIL:WRONG_ACCOUNT"
    return "FAIL:WRONG_AMOUNT" if m_cents != e_cents else "FAIL:MISMATCH"
