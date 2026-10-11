"""Gates G0–G6, clean checkouts, anti-gaming checks and the pre-commit hook (spec §5.11), on
the Ledger practice repository with its disclosed practice regression."""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path

import pytest

from kintsugi.crew.casefile import CaseFile
from kintsugi.crew.llm import ScriptedModel
from kintsugi.gates import antigaming, g0, g1, g2, g3, g4, g5, g6, precommit
from kintsugi.gates.checkout import (
    MergeConflict,
    changed_files,
    clean_checkout,
    diff_text,
    merged_checkout,
)
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target, load_target
from kintsugi.sandbox import proc
from tests.helpers import practice as pr


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str | Path]:
    p = pr.make_repo(tmp_path_factory.mktemp("practice") / "repo")
    buggy = pr.plant_refactor(p.repo)
    pr.git(p.repo, "branch", "buggy", buggy)
    fixed = pr.hoist_fix(p.repo)
    pr.git(p.repo, "checkout", "-q", "-b", "lookup", buggy)
    lookup = pr.lookup_fix(p.repo)
    pr.git(p.repo, "checkout", "-q", "main")
    return {"repo": p.repo, "healthy": p.healthy, "buggy": buggy, "fixed": fixed,
            "lookup": lookup, "identity": p.identity}  # fmt: skip


def _target(repo: dict[str, str | Path]) -> Target:
    return load_target(Path(repo["repo"]), pr.HOST)


def test_g0_reproduces_the_regression_and_g2_accepts_only_the_fix(repo) -> None:
    root, target = Path(repo["repo"]), _target(repo)
    train = importlib.import_module("practice_ledger.evals.generate").tasks("train")
    with clean_checkout(root, str(repo["buggy"])) as buggy:
        live = run_tasks(target, buggy, train, seed=1)
        failing = live.failed()
        assert failing, "the practice regression should fail some train tasks at seed 1"
        assert {live.verdicts()[t] for t in failing} == {"FAIL:DUPLICATE_ENTRY"}
        chosen = [t for t in train if t["id"] in failing[:3]]
        repro = g0.reproduce(target, buggy, chosen, seed=1, split="train")
        assert repro.ok, repro.result.summary
        assert repro.result.evidence["replays_failing"] == 3
        assert repro.cassette is not None
        still = g2.reproduction_fixed(target, buggy, repro.cassette, chosen, repro.failing,
                                      seed=1, split="train")  # fmt: skip
        assert not still.passed
    with clean_checkout(root, str(repo["fixed"])) as fixed:
        result = g2.reproduction_fixed(target, fixed, repro.cassette, chosen, repro.failing,
                                       seed=1, split="train")  # fmt: skip
        assert result.passed, result.summary
    with clean_checkout(root, str(repo["healthy"])) as healthy:
        assert not g0.reproduce(target, healthy, chosen, seed=1, split="train").ok


def test_g4_accepts_the_hoist_and_rejects_the_costly_lookup(repo) -> None:
    root, target = Path(repo["repo"]), _target(repo)
    identity = str(repo["identity"])
    with (clean_checkout(root, str(repo["buggy"])) as base,
          clean_checkout(root, str(repo["fixed"])) as fixed,
          clean_checkout(root, str(repo["lookup"])) as lookup):  # fmt: skip
        cache: dict = {}  # the base runs are shared by both candidates, as in one round
        good = g4.held_out_efficacy(target, base, fixed, identity, seeds=[1, 2],
                                    base_cache=cache, base_key="buggy")  # fmt: skip
        costly = g4.held_out_efficacy(target, base, lookup, identity, seeds=[1, 2],
                                      base_cache=cache, base_key="buggy")  # fmt: skip
    assert good.passed, good.summary
    assert good.evidence["b"] > 5 >= good.evidence["c"]
    assert good.evidence["p"] < 0.05
    assert not costly.passed
    assert "cost" in costly.summary
    blob = json.dumps([good.as_dict(), costly.as_dict()])
    assert "HO-0" not in blob  # aggregates only: no holdout task id leaves G4 (I5)
    with pytest.raises(g4.HoldoutLeak):
        g4.check_no_leak(g4.failed("G4", "HO-001 leaked"), "canary-words", ["HO-001"])


def test_g1_runs_lint_and_typecheck(repo) -> None:
    root, target = Path(repo["repo"]), _target(repo)
    with clean_checkout(root, str(repo["fixed"])) as fixed:
        assert g1.build_static(target, fixed).passed
        (fixed / "practice_ledger" / "broken.py").write_text("import os\nx: int = 'a'\n",
                                                             encoding="utf-8")  # fmt: skip
        result = g1.build_static(target, fixed)
    assert not result.passed
    assert "ruff" in result.summary


def test_g3_parses_verify_and_flags_flips_without_entire(repo) -> None:
    text = ("NEWLY FAILING (2): tests/a.py::t1, tests/a.py::t2\n"
            "VERDICT: REGRESSION in 2 tests: tests/a.py::t1, tests/a.py::t2\n")  # fmt: skip
    assert g3.parse_verify(text) == (
        ["tests/a.py::t1", "tests/a.py::t2"],
        [],
        "REGRESSION in 2 tests: tests/a.py::t1, tests/a.py::t2",
    )
    root, target = Path(repo["repo"]), _target(repo)
    with clean_checkout(root, str(repo["buggy"])) as base:
        before = g3.pytest_results(target, base)
        assert before
        assert all(outcome == "PASSED" for outcome in before.values())
        reconcile = base / "practice_ledger" / "reconcile.py"
        reconcile.write_text(reconcile.read_text(encoding="utf-8").replace(
            "self.cursor += 1", "self.cursor += 2"), encoding="utf-8")  # fmt: skip
        after = g3.pytest_results(target, base)
    flips = g3.pytest_flips(before, after)
    assert not flips.passed
    assert "tests/test_reconcile.py::test_resume_after_partial_batch" in flips.evidence["flips"]


@pytest.mark.contract
def test_g3_with_entire_graph_verify(repo, tmp_path: Path) -> None:
    root, target = Path(repo["repo"]), _target(repo)
    baseline = tmp_path / "baseline.json"
    with clean_checkout(root, str(repo["buggy"])) as base:
        g3.record_baseline(target, base, baseline)
    with clean_checkout(root, str(repo["fixed"])) as fixed:
        assert g3.no_collateral(target, fixed, baseline).passed
        reconcile = fixed / "practice_ledger" / "reconcile.py"
        reconcile.write_text(reconcile.read_text(encoding="utf-8").replace(
            "self.cursor += 1", "self.cursor += 2"), encoding="utf-8")  # fmt: skip
        flipped = g3.no_collateral(target, fixed, baseline)
    assert not flipped.passed
    assert any("test_resume_after_partial_batch" in t for t in flipped.evidence["flips"])


def test_g5_ranks_down_a_revert_and_discards_invented_quotes(repo, tmp_path: Path) -> None:
    root = Path(repo["repo"])
    originating = diff_text(root, str(repo["healthy"]), str(repo["buggy"]))
    revert = diff_text(root, str(repo["buggy"]), str(repo["healthy"]))
    hoist = diff_text(root, str(repo["buggy"]), str(repo["fixed"]))
    assert g5.revert_share(revert, originating) == pytest.approx(1.0)
    assert g5.revert_share(hoist, originating) < g5.REVERT_SHARE
    record = {"stated_goal": "readability",
              "behavioral_contract": [{"clause": "no behavior change",
                                       "quote": "keep behavior identical"}]}  # fmt: skip
    case = CaseFile(tmp_path, "INC-G5")
    offline = g5.judge(None, case, hypothesis="H1", title="revert", record=record,
                       patch=revert, originating=originating)  # fmt: skip
    assert offline.status == "warn"
    model = ScriptedModel({"auditor_g5": {"judgements": [
        {"clause": "no behavior change", "quote": "keep behavior identical",
         "verdict": "kept", "reason": "hoist keeps behavior"},
        {"clause": "invented", "quote": "words nobody wrote", "verdict": "violated",
         "reason": "made up"}]}})  # fmt: skip
    judged = g5.judge(model, case, hypothesis="H2", title="hoist", record=record, patch=hoist,
                      originating=originating)  # fmt: skip
    assert judged.status == "pass"
    assert judged.evidence["discarded"][0]["quote"] == "words nobody wrote"
    calls = case.calls()
    assert all("surgeon" not in item["source"] for c in calls
               for item in c["manifest"]["included"])  # fmt: skip
    assert g5.decide({"judgements": [{"clause": "no behavior change",
                                      "quote": "keep behavior identical",
                                      "verdict": "violated", "reason": "x"}]},
                     record, provider="t").status == "fail"  # fmt: skip


def test_g6_compares_spend_with_the_budget() -> None:
    assert g6.within_budget(4.0, 12.4).passed
    assert not g6.within_budget(12.5, 12.4).passed


def test_antigaming_blocks_protected_paths_big_diffs_and_network(repo, tmp_path: Path) -> None:
    root = Path(repo["repo"])
    clean = antigaming.check(root, str(repo["buggy"]), str(repo["fixed"]))
    assert clean.result.passed, clean.result.summary
    pr.git(root, "checkout", "-q", "-b", "gaming", str(repo["buggy"]))
    try:
        test = root / "tests" / "test_reconcile.py"
        test.write_text(test.read_text(encoding="utf-8") + "\n# weakened\n", encoding="utf-8")
        tampered = pr.commit_all(root, "tamper with a test")
        result = antigaming.check(root, str(repo["buggy"]), tampered)
        assert not result.result.passed
        assert "protected paths" in result.result.summary
        big = root / "practice_ledger" / "big.py"
        big.write_text("".join(f"X{i} = {i}\n" for i in range(130)), encoding="utf-8")
        large = pr.commit_all(root, "a large change")
        over = antigaming.check(root, tampered, large)
        assert "budget" in over.result.summary
        assert antigaming.check(root, tampered, large, approved_large_diff=True).result.passed
        net = root / "practice_ledger" / "net.py"
        net.write_text("import httpx\n\nURL = 'https://example.invalid/rates'\n",
                       encoding="utf-8")  # fmt: skip
        networked = pr.commit_all(root, "call out")
        found = antigaming.check(root, large, networked)
        assert "network" in found.result.summary
        assert len(found.network) == 2
    finally:
        pr.git(root, "checkout", "-q", "main")


def test_the_precommit_hook_refuses_protected_paths_on_surgeon_branches(tmp_path: Path) -> None:
    repo = tmp_path / "hooked"
    shutil.copytree(pr.FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    pr.git(repo, "init", "-q", "-b", "main")
    pr.commit_all(repo, "init")
    hooks = precommit.hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    assert precommit.install(repo).name == "pre-commit"
    assert precommit.installed(repo)
    assert (hooks / precommit.CHAINED).is_file()  # an existing hook is kept and chained
    wt = tmp_path / "wt-H1"
    pr.git(repo, "worktree", "add", "-q", "-b", "kintsugi/INC-0001/H1", str(wt))
    test = wt / "tests" / "test_reconcile.py"
    test.write_text(test.read_text(encoding="utf-8") + "\n# weakened\n", encoding="utf-8")
    pr.git(wt, "add", "-A")
    refused = proc.run(["git", "-C", str(wt), *pr.IDENTITY, "commit", "-q", "-m", "x"],
                       timeout_s=120)  # fmt: skip
    assert refused.returncode != 0
    assert "protected" in refused.stderr
    pr.git(wt, "reset", "-q", "--hard")
    code = wt / "practice_ledger" / "retry.py"
    code.write_text(code.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    pr.git(wt, "add", "-A")
    assert proc.run(["git", "-C", str(wt), *pr.IDENTITY, "commit", "-q", "-m", "ok"],
                    timeout_s=120).ok  # fmt: skip
    main_test = repo / "tests" / "test_reconcile.py"  # other branches are not guarded
    main_test.write_text(main_test.read_text(encoding="utf-8") + "\n# fine\n", encoding="utf-8")
    pr.git(repo, "add", "-A")
    assert proc.run(["git", "-C", str(repo), *pr.IDENTITY, "commit", "-q", "-m", "main"],
                    timeout_s=120).ok  # fmt: skip


def test_checkouts_are_clean_and_merges_report_conflicts(repo) -> None:
    root = Path(repo["repo"])
    with clean_checkout(root, str(repo["healthy"])) as path:
        assert "def attempt()" in (path / pr.RECONCILE).read_text(encoding="utf-8")
    assert changed_files(root, str(repo["healthy"]), str(repo["buggy"])) == [pr.RECONCILE]
    with merged_checkout(root, str(repo["fixed"]), str(repo["buggy"])) as merged:
        assert "outside the retried scope" in (merged / pr.RECONCILE).read_text(encoding="utf-8")
    pr.git(root, "checkout", "-q", "-b", "rival", str(repo["buggy"]))
    try:
        text = (root / pr.RECONCILE).read_text(encoding="utf-8")
        pr.write(root, pr.RECONCILE, text.replace(pr.NESTED, pr.HOISTED.replace(
            "outside the retried scope", "built once per call")))  # fmt: skip
        rival = pr.commit_all(root, "a rival hoist")
    finally:
        pr.git(root, "checkout", "-q", "main")
    with pytest.raises(MergeConflict), merged_checkout(root, rival, str(repo["fixed"])):
        pass
