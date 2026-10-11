"""Labels, duplicate link and route as they landed on GitHub (spec §5.9.3)."""

from __future__ import annotations

import re
from typing import Any

_DUPLICATE = re.compile(r"Duplicate of #(\d+)")


def _one(labels: list[str], prefix: str) -> str | None:
    found = [label.removeprefix(prefix) for label in labels if label.startswith(prefix)]
    return found[0] if len(found) == 1 else None


def grade(task: dict[str, Any], output: dict[str, Any]) -> str:
    if "error" in output:
        return f"FAIL:{output['error']}"
    gold = task["gold"]
    labels = [str(label) for label in output.get("labels") or []]
    comments = [str(comment) for comment in output.get("comments") or []]
    if len(comments) != len(set(comments)):
        return "FAIL:DUPLICATE_COMMENT"
    if _one(labels, "type/") != gold["type"]:
        return "FAIL:WRONG_TYPE"
    if _one(labels, "area/") != gold["component"]:
        return "FAIL:WRONG_COMPONENT"
    if _one(labels, "team/") != gold["team"]:
        return "FAIL:WRONG_ROUTE"
    links = [int(m[1]) for c in comments if (m := _DUPLICATE.search(c))]
    marked = "duplicate" in labels or bool(links)
    if gold["duplicate_of"] is None:
        return "FAIL:FALSE_DUPLICATE" if marked else "PASS"
    if not marked:
        return "FAIL:MISSED_DUPLICATE"
    return "PASS" if links == [gold["duplicate_of"]] else "FAIL:WRONG_DUPLICATE"
