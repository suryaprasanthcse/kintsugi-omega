"""The canary's per-host H0 and its batch records (spec §5.12, §6.5 step 11, §6.7 step 15)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from kintsugi.gates import canary
from kintsugi.gates.evals import EvalResult, TaskRun
from kintsugi.gates.target import load_target
from kintsugi.stats.sprt import Decision

OMEGA = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("host", "p0"),
    [("ledger", 0.0158), ("scout", 0.031), ("triage", 0.0972)],
)
def test_p0_is_the_wilson_upper_bound_of_the_recorded_train_failure_rate(
    host: str, p0: float
) -> None:
    baseline = canary.baseline_p0(host)
    assert baseline.p0 == p0
    assert f"baselines/{host}.json train" in baseline.source
    sprt = canary.sprt_for(baseline)
    assert 0 < sprt.p0 < sprt.p1 < 1


def test_hosts_without_a_baseline_use_the_control_plane_default(tmp_path: Path) -> None:
    baseline = canary.baseline_p0("ledger-practice", baselines=tmp_path)
    assert baseline.p0 == canary.DEFAULT_P0
    assert "no baseline" in baseline.source


def test_scout_canary_rejects_a_duplicate_citation_regression_quickly() -> None:
    # §6.7's SPRT path on Scout's own H0: a regression failing every other run is caught fast,
    # a clean canary needs many runs (H0 is a small failure rate) and never rejects.
    sprt = canary.sprt_for(canary.baseline_p0("scout"))
    bad = canary.decide(sprt, [[True, False, True, True, False, True, False, True]])
    assert bad.decision is Decision.REJECT
    assert bad.n <= 8
    clean = canary.decide(sprt, [[False] * 10 for _ in range(12)])
    assert clean.decision is Decision.ACCEPT
    assert clean.n == sprt.runs_to_accept_without_failures()


def test_batches_rotate_through_the_train_pool() -> None:
    target = load_target(OMEGA, "scout")
    first, second = canary.batch_tasks(target, 0), canary.batch_tasks(target, 1)
    assert len(first) == len(second) == canary.BATCH_TASKS
    assert not {t["id"] for t in first} & {t["id"] for t in second}


def test_sample_records_every_run_for_trace_to_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    @contextmanager
    def checkout(repo: Path, sha: str):
        seen["sha"] = sha
        yield tmp_path

    def run_tasks(target, checkout, tasks, *, seed, phase):
        seen.update(seed=seed, phase=phase)
        verdicts = ["PASS", "FAIL:DUPLICATE_CITATION"] * (len(tasks) // 2)
        return EvalResult([TaskRun(t["id"], seed, v, 0.1, 0.0)
                           for t, v in zip(tasks, verdicts, strict=True)])  # fmt: skip

    monkeypatch.setattr(canary, "clean_checkout", checkout)
    monkeypatch.setattr(canary, "run_tasks", run_tasks)
    target = load_target(OMEGA, "scout")
    record = tmp_path / "canary"
    runs = canary.sample(target, "merge-sha", 3, record=record)
    assert seen == {"sha": "merge-sha", "seed": canary.SEED_BASE + 3, "phase": "canary"}
    assert [r.passed for r in runs[:2]] == [True, False]
    entry = json.loads((record / "batch-003.json").read_text(encoding="utf-8"))
    assert entry["host"] == "scout"
    assert entry["seed"] == canary.SEED_BASE + 3
    failing = canary.failing_runs(record)
    assert len(failing) == canary.BATCH_TASKS // 2
    assert {verdict for _, _, verdict in failing} == {"FAIL:DUPLICATE_CITATION"}
