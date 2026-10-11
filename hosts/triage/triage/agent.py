"""Triage's task entry point: one incoming issue in, labels and comments on GitHub out."""

from __future__ import annotations

from typing import Any

from neatlogs import span

from triage.tools import (
    apply_label,
    classify_component,
    classify_type,
    comment,
    detect_duplicate,
    issue_state,
    routing,
    search_issues,
)


@span(kind="WORKFLOW", name="triage_issue")
def run_task(task: dict[str, Any]) -> dict[str, Any]:
    issue = task["issue"]
    config = routing()
    steps, teams = config["steps"], config["teams"]
    kind = classify_type(issue, steps["classify_type"])
    component = classify_component(issue, steps["classify_component"])
    candidates = search_issues(f"{issue['title']} {issue['body']}")
    duplicate = detect_duplicate(issue, candidates, steps["detect_duplicate"])
    number = int(issue["number"])
    apply_label(number, f"type/{kind}")
    apply_label(number, f"area/{component}")
    apply_label(number, f"team/{teams[component]}")
    if duplicate is not None:
        apply_label(number, "duplicate")
        comment(number, f"Duplicate of #{duplicate}")
    state = issue_state(number)
    return {"labels": state["labels"], "comments": state["comments"]}
