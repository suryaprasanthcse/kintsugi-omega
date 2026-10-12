"""The canary (spec §5.11 row C, §5.12, §6.5 step 11, §6.7 steps 15–16).

After a merge, live traffic decides: a Wald SPRT on the host's task outcomes, batch by batch,
with H0 "the failure rate is the host's baseline p0" against H1 "it doubled". The workflow
(``kintsugi.control.workflows.Canary``) owns the sequential test and its durable timers; this
module owns what the workflow cannot do deterministically:

- **p0 per host**, from the host's recorded baseline (``evidence/baselines/<host>.json``): the
  Wilson 95% upper bound of the train split's failure rate. The fleet's baselines pass 100% of
  their train tasks under the background chaos profile, so the observed rate (0) cannot be H0
  (the SPRT needs 0 < p0 < p1 < 1); the upper bound is the largest rate the baseline does not
  rule out, which keeps a clean canary from reverting a correct fix. Hosts without a recorded
  baseline (the disclosed practice hosts) fall back to the control plane's default.
- **A batch**: ``n`` train tasks of the merged code at seed ``5000 + batch`` under the
  background profile (live traffic stand-in), rotating through the train pool.
- **A record per batch** (task ids, verdicts, seed), so the canary's failing traces can become
  a regression cassette (trace-to-test, §6.7 step 18) without re-running anything blind.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import TaskRun, run_tasks
from kintsugi.gates.target import Target
from kintsugi.stats.sprt import SPRT, SPRTResult

OMEGA = Path(__file__).resolve().parents[2]
BASELINES = OMEGA / "evidence" / "baselines"
BATCH_TASKS = 10
SEED_BASE = 5000
DEFAULT_P0 = 0.05  # ControlConfig.canary_p0: hosts without a recorded baseline
RATIO = 2.0  # H1 = 2·p0 (§5.12)


@dataclass(frozen=True)
class HostBaseline:
    host: str
    p0: float
    source: str  # where p0 came from, recorded with every canary


def baseline_p0(host: str, *, baselines: Path = BASELINES, split: str = "train") -> HostBaseline:
    """H0's failure rate for ``host``: the Wilson 95% upper bound of its baseline failure rate."""
    path = baselines / f"{host}.json"
    if not path.is_file():
        return HostBaseline(host, DEFAULT_P0, f"default {DEFAULT_P0} (no baseline for {host})")
    data = json.loads(path.read_text(encoding="utf-8"))
    stats = (data.get("per_split") or {}).get(split) or data["pooled"]
    low = float(stats["wilson95"][0])
    p0 = round(min(max(1.0 - low, 0.005), 0.45), 4)  # p1 = 2·p0 must stay below 1
    observed = 1.0 - float(stats["pass_rate"])
    return HostBaseline(
        host,
        p0,
        f"baselines/{path.name} {split}: {stats['passed']}/{stats['n']} passed, failure rate"
        f" {observed:.4f}, Wilson95 upper {1.0 - low:.4f} (recorded {data.get('recorded_at')})",
    )


def sprt_for(baseline: HostBaseline, *, ratio: float = RATIO) -> SPRT:
    return SPRT.regression(baseline.p0, ratio=ratio)


def batch_tasks(target: Target, batch: int, *, n: int = BATCH_TASKS) -> list[dict[str, Any]]:
    target.make_importable()
    pool = importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")
    start = (batch * n) % max(1, len(pool))
    return (pool[start:] + pool[:start])[:n]


def sample(
    target: Target,
    sha: str,
    batch: int,
    *,
    n: int = BATCH_TASKS,
    seed_base: int = SEED_BASE,
    record: Path | None = None,
) -> list[TaskRun]:
    """One live-traffic batch on the merged code (background chaos profile)."""
    tasks = batch_tasks(target, batch, n=n)
    seed = seed_base + batch
    with clean_checkout(target.repo, sha) as checkout:
        runs = run_tasks(target, checkout, tasks, seed=seed, phase="canary").runs
    if record is not None:
        record.mkdir(parents=True, exist_ok=True)
        entry = {
            "host": target.id,
            "sha": sha,
            "batch": batch,
            "seed": seed,
            "runs": [{"task": r.task, "verdict": r.verdict} for r in runs],
        }
        (record / f"batch-{batch:03d}.json").write_text(
            json.dumps(entry, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    return runs


def records(record: Path) -> list[dict[str, Any]]:
    """Every recorded batch of one canary, in order."""
    if not record.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(record.glob("batch-*.json"))]


def failing_runs(record: Path) -> list[tuple[int, str, str]]:
    """``(seed, task, verdict)`` of every failed canary run: the trace-to-test input."""
    return [
        (int(entry["seed"]), str(run["task"]), str(run["verdict"]))
        for entry in records(record)
        for run in entry["runs"]
        if run["verdict"] != "PASS"
    ]


def decide(sprt: SPRT, batches: Iterable[Iterable[bool]]) -> SPRTResult:
    """The workflow's sequential decision, recomputed from recorded outcomes (evidence)."""
    return sprt.run(failed for batch in batches for failed in batch)


def summary(baseline: HostBaseline, result: SPRTResult) -> Mapping[str, Any]:
    sprt = sprt_for(baseline)
    return {
        "host": baseline.host,
        "p0": baseline.p0,
        "p1": sprt.p1,
        "A": round(sprt.upper, 3),
        "B": round(sprt.lower, 3),
        "decision": str(result.decision),
        "n": result.n,
        "llr": round(result.llr, 4),
        "p0_source": baseline.source,
    }
