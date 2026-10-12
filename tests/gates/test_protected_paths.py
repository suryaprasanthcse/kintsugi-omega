from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kintsugi.gates.protected_paths import ESCAPE, glob_regex, load_policy, normalise

REPO = Path(__file__).resolve().parents[2]
POLICY = load_policy()


def _hits(paths: list[str], scope: str) -> list[str]:
    return [v.path for v in POLICY.violations(paths, [scope])]


def test_policy_loads_with_the_spec_diff_budget() -> None:
    assert POLICY.diff_budget_lines == 120
    assert set(POLICY.scopes) == {"surgeon", "self_repair", "eval_augment"}


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("hosts/scout/scout/evals/regressions/ab_1_scout_canary.json", False),
        ("scout/evals/regressions/x.json", False),
        ("hosts/scout/scout/research.py", True),  # never code
        ("hosts/scout/scout/evals/holdout/tasks.jsonl.age", True),  # never the holdout (I5)
        ("hosts/scout/tests/regressions.json", True),
        ("hosts/scout/scout/graders/cases.json", True),
    ],
)
def test_eval_augment_adds_regression_data_only(path: str, protected: bool) -> None:
    assert bool(_hits([path], "eval_augment")) is protected
    assert POLICY.over_budget(121)
    assert not POLICY.over_budget(120)


@pytest.mark.parametrize(
    "path",
    [
        "hosts/ledger/tests/test_reconcile.py",
        "tests/test_cli.py",
        "hosts/scout/evals/train.jsonl",
        "hosts/ledger/graders/journal.py",
        "hosts/ledger/holdout/tasks.jsonl",
        "hosts/ledger/evals/holdout.age",
        ".github/workflows/omega-ci.yml",
        "hosts/triage/.claude/settings.json",
        ".entire/settings.json",
        "CLAUDE.md",
        "hosts/ledger/AGENTS.md",
        ".kintsugi/protected_paths.yaml",
        "hosts\\ledger\\tests\\test_x.py",
    ],
)
def test_surgeons_cannot_touch_what_defines_success(path: str) -> None:
    assert _hits([path], "surgeon") != []


@pytest.mark.parametrize(
    "path",
    [
        "hosts/ledger/ledger/reconcile.py",
        "hosts/ledger/ledger/util/retry.py",
        "hosts/scout/scout/search.py",
        "hosts/contests/app.py",
        "hosts/ledger/tests.py",
        "config/routing.yaml",
    ],
)
def test_ordinary_code_is_editable(path: str) -> None:
    assert _hits([path], "surgeon") == []


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("kintsugi/proof/ledger.py", True),
        ("kintsugi/stats/sprt.py", True),
        ("kintsugi/gates/protected_paths.py", True),
        ("kintsugi/sandbox/proc.py", True),
        ("schemas/E2_ledger_entry.schema.json", True),
        ("docs/EXECUTION_PLAN.md", True),
        ("kintsugi/crew/pathologist.py", False),
        ("kintsugi/sandbox/runner.py", False),
        ("kintsugi/proofs/other.py", False),
    ],
)
def test_self_repair_cannot_weaken_verification(path: str, protected: bool) -> None:
    assert bool(_hits([path], "self_repair")) is protected


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "C:\\Windows\\x", "a/../../b"])
def test_paths_escaping_the_worktree_are_violations(path: str) -> None:
    [violation] = POLICY.violations([path], ["surgeon"])
    assert violation.pattern == ESCAPE


def test_normalisation() -> None:
    assert normalise("a/./b/../c.py") == "a/c.py"
    assert normalise("hosts\\ledger\\x.py") == "hosts/ledger/x.py"
    assert normalise("..") is None


def test_one_violation_per_scope_per_path() -> None:
    violations = POLICY.violations(["hosts/ledger/evals/holdout.age"], ["surgeon", "self_repair"])
    assert [(v.scope, v.pattern) for v in violations] == [("surgeon", "**/evals/**")]


def test_unknown_scopes_are_an_error() -> None:
    with pytest.raises(KeyError, match="unknown"):
        POLICY.violations(["x.py"], ["nobody"])


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("**/tests/**", "tests", True),
        ("**/tests/**", "a/b/tests/c/d.py", True),
        ("**/tests/**", "contests/a.py", False),
        ("*.py", "a.py", True),
        ("*.py", "dir/a.py", False),
        ("kintsugi/proof/**", "kintsugi/proof", True),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file/.txt", False),
    ],
)
def test_glob_semantics(pattern: str, path: str, matches: bool) -> None:
    assert bool(glob_regex(pattern).fullmatch(path)) is matches


def test_every_package_marked_protected_is_covered_by_the_policy() -> None:
    marked = []
    for init in sorted((REPO / "kintsugi").glob("*/__init__.py")):
        docstring = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8"))) or ""
        if "Protected for self-repair" in docstring:
            marked.append(f"{init.parent.relative_to(REPO).as_posix()}/module.py")
    assert marked, "expected proof, stats and gates to be marked"
    assert all(_hits([path], "self_repair") for path in marked), marked
