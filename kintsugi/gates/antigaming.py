"""Anti-gaming checks on a candidate's diff (spec §5.11), run by the Auditor before any gate.

1. **Protected paths**, the Auditor's half of "enforced twice" (the other half is the worktree
   pre-commit hook, ``kintsugi.gates.precommit``): tests, evals, graders, holdouts, CI and agent
   configuration (``.kintsugi/protected_paths.yaml``, scope ``surgeon``).
2. **Diff budget**: at most 120 changed lines unless the Coordinator approved more for this card
   (the approval is recorded on the card).
3. **No new outbound network calls**: added lines are scanned for HTTP clients, sockets and URL
   literals, and ``entire graph diff`` is consulted for new ``HTTP_CALLS`` relations when the
   graph is available. A fix must never reach the world the gates replay.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.gates.checkout import changed_files, changed_lines, diff_text
from kintsugi.gates.common import GateResult, failed, passed
from kintsugi.gates.protected_paths import Policy, load_policy

NETWORK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("http client import",
         r"^\s*(import|from)\s+(requests|httpx|aiohttp|urllib3|websockets)\b"),
        ("stdlib networking import",
         r"^\s*(import|from)\s+(socket|http\.client|urllib\.request|ftplib|smtplib)\b"),
        ("http client call",
         r"\b(requests|httpx|aiohttp)\.(get|post|put|patch|delete|head|request|Client|"
         r"AsyncClient|Session|ClientSession)\b"),
        ("socket use", r"\bsocket\.(socket|create_connection)\b"),
        ("urlopen", r"\burlopen\("),
        ("url literal", r"[\"']https?://"),
    )
)  # fmt: skip


@dataclass(frozen=True)
class DiffCheck:
    result: GateResult
    paths: tuple[str, ...]
    lines: int
    network: tuple[str, ...] = field(default=())


def added_lines(diff: str) -> list[tuple[str, str]]:
    """``(path, line)`` for every added line of a unified diff."""
    path, out = "", []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, line[1:]))
    return out


def network_findings(diff: str, graph_diff: dict[str, Any] | None = None) -> list[str]:
    found = []
    for path, line in added_lines(diff):
        if not path.endswith(".py"):
            continue
        for name, pattern in NETWORK_PATTERNS:
            if pattern.search(line):
                found.append(f"{path}: {name}: {line.strip()[:120]}")
                break
    for entry in (graph_diff or {}).get("files") or []:
        for change in entry.get("changes") or []:
            if "HTTP_CALLS" in str(change.get("relations") or change.get("type") or ""):
                where, name = entry.get("path"), change.get("name")
                found.append(f"{where}: graph: new HTTP_CALLS from {name}")
    return found


def check(
    repo: Path,
    base: str,
    head: str,
    *,
    policy: Policy | None = None,
    scopes: Iterable[str] = ("surgeon",),
    approved_large_diff: bool = False,
    graph_diff: dict[str, Any] | None = None,
) -> DiffCheck:
    rules = policy or load_policy()
    paths = changed_files(repo, base, head)
    lines = changed_lines(repo, base, head)
    problems: list[str] = []
    violations = rules.violations(paths, list(scopes))
    if violations:
        problems.append("protected paths: " + ", ".join(f"{v.path} ({v.pattern})"
                                                        for v in violations))  # fmt: skip
    if rules.over_budget(lines) and not approved_large_diff:
        problems.append(f"diff of {lines} lines exceeds the {rules.diff_budget_lines}-line budget")
    network = network_findings(diff_text(repo, base, head), graph_diff)
    if network:
        problems.append("new outbound network calls: " + "; ".join(network[:5]))
    evidence = {"paths": paths, "lines": lines, "approved_large_diff": approved_large_diff}
    result = (failed("diff", "; ".join(problems), **evidence) if problems
              else passed("diff", f"{len(paths)} files, {lines} lines, no protected paths",
                          **evidence))  # fmt: skip
    return DiffCheck(result, tuple(paths), lines, tuple(network))
