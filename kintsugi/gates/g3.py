"""G3, no collateral damage (spec §5.11): zero pass→fail flips in the repository's own tests.

``entire graph verify --test "<pytest -rA …>" --pre-edit-baseline`` compares a run on the
candidate with a baseline recorded on the incident's base commit and reports which tests newly
fail. The command prints per-test results (``-rA``): with ``-q`` the plugin falls back to an
exit-code verdict that cannot tell a pre-existing failure from a new one (amendment A-48).
Where Entire is not installed (GitHub Actions, G7) :func:`pytest_flips` computes the same
delta from two ``pytest -rA`` runs.

The flipped test names (and their assertion messages) go back to the Surgeon: they are the
repository's visible tests, never holdout tasks.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

from kintsugi.gates.common import GateResult, failed, passed
from kintsugi.gates.target import Target
from kintsugi.memory import graph
from kintsugi.sandbox import proc

_NEWLY_FAILING = re.compile(r"^NEWLY FAILING \((\d+)\):\s*(.*)$", re.MULTILINE)
_NEWLY_PASSING = re.compile(r"^NEWLY PASSING \((\d+)\):\s*(.*)$", re.MULTILINE)
_VERDICT = re.compile(r"^VERDICT:\s*(.*)$", re.MULTILINE)
_RESULT = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)\s+(\S+::\S+)", re.MULTILINE)


def verify_command(target: Target, tests: Sequence[str] | None = None) -> str:
    python = Path(sys.executable).as_posix()
    selected = " ".join(tests) if tests else target.tests
    return f"{python} -m pytest -rA -p no:cacheprovider -q {selected}"


def record_baseline(target: Target, checkout: Path, baseline: Path) -> str:
    return graph.verify(checkout, verify_command(target), record_baseline=baseline,
                        extra_env=target.code_env(checkout))  # fmt: skip


def parse_verify(text: str) -> tuple[list[str], list[str], str]:
    """(newly failing ids, newly passing ids, verdict line) from ``entire graph verify``."""

    def ids(pattern: re.Pattern[str]) -> list[str]:
        match = pattern.search(text)
        return [i.strip() for i in match.group(2).split(",") if i.strip()] if match else []

    verdict = _VERDICT.search(text)
    return ids(_NEWLY_FAILING), ids(_NEWLY_PASSING), verdict.group(1).strip() if verdict else ""


def no_collateral(
    target: Target, checkout: Path, baseline: Path, *, tests: Sequence[str] | None = None
) -> GateResult:
    text = graph.verify(checkout, verify_command(target, tests), pre_edit_baseline=baseline,
                        extra_env=target.code_env(checkout))  # fmt: skip
    flips, fixed, verdict = parse_verify(text)
    match = _NEWLY_FAILING.search(text)
    count = int(match.group(1)) if match else 0
    if not verdict:
        return failed("G3", "entire graph verify gave no verdict", output=text[-1500:])
    if "exit-code only" in text or "not recognised" in text:
        return failed("G3", "the test runner's output was not parsed", output=text[-1500:])
    evidence = {"verdict": verdict, "flips": flips, "newly_passing": fixed}
    if count:
        return failed("G3", f"{count} pass→fail flip(s)", **evidence)
    return passed("G3", "zero pass→fail flips", **evidence)


def pytest_results(target: Target, checkout: Path, *, timeout_s: float = 1800.0) -> dict[str, str]:
    """Per-test outcomes of ``pytest -rA`` (the CI fallback for :func:`no_collateral`)."""
    argv = [sys.executable, "-m", "pytest", "-rA", "-p", "no:cacheprovider", "-q", target.tests]
    result = proc.run(argv, cwd=checkout, timeout_s=timeout_s, extra_env=target.code_env(checkout))
    return {test: outcome for outcome, test in _RESULT.findall(result.stdout)}


def pytest_flips(before: dict[str, str], after: dict[str, str]) -> GateResult:
    flips = sorted(t for t, outcome in before.items()
                   if outcome == "PASSED" and after.get(t) != "PASSED")  # fmt: skip
    if flips:
        return failed("G3", f"{len(flips)} pass→fail flip(s)", flips=flips)
    return passed("G3", "zero pass→fail flips", tests=len(after))
