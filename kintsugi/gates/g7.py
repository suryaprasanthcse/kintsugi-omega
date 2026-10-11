"""G7, independent CI (spec §5.11): G1–G4 re-run in GitHub Actions with a fresh seed.

``.github/workflows/g7-gates.yml`` calls ``kintsugi gates g7`` on the pull request's base and
head. Everything is re-derived from the repository and the fresh seed, nothing is taken from
the Auditor's local run:

- **G1** build and static checks on the head;
- **G2** the incident's repro tasks (named in the PR body, public train tasks) at three fresh
  seeds under the target's stress profile: the incident's failure class (``Kintsugi-Symptom``,
  the error class of its signature φ) must not occur on the head at any seed, and the head may
  not fail more often than the base for any other reason under the same seeds. A harsh fault
  profile can exhaust a correct retry budget (``RateLimited``); that is the weather, not the
  symptom, so "pass 3/3" would reject correct fixes at random (amendment A-53). Without a
  symptom the strict 3/3 criterion applies;
- **G3** per-test outcomes of the base and the head (``pytest -rA``): zero pass→fail flips;
- **G4** the paired holdout evaluation at three fresh seeds, with the holdout identity from the
  Actions secret; only aggregates are printed (public logs).

Exit 0 only when all four pass. The JSON summary is aggregate-only by construction.
"""

from __future__ import annotations

import importlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from kintsugi.gates import g1, g3, g4
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.common import GateResult, failed, passed
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.senses.signature import canonical_error

REPLAYS = 3


def repro_fresh(
    target: Target,
    base: Path,
    head: Path,
    task_ids: list[str],
    seed: int,
    symptom: str | None = None,
) -> GateResult:
    if not task_ids:
        return failed("G2", "the PR names no repro tasks (Kintsugi-Repro-Tasks)")
    target.make_importable()
    pool = {
        t["id"]: t
        for t in importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")
    }
    tasks = [pool[t] for t in task_ids if t in pool]
    profile = target.stress() or chaos.background_profile()
    head_runs, base_runs = [], []
    for s in range(seed, seed + REPLAYS):
        head_runs += run_tasks(target, head, tasks, seed=s, profile=profile, phase="g7").runs
        base_runs += run_tasks(target, base, tasks, seed=s, profile=profile, phase="g7").runs

    def classes(runs: list[Any]) -> Counter[str]:
        return Counter(canonical_error(r.verdict) for r in runs if not r.passed)

    head_failed, base_failed = classes(head_runs), classes(base_runs)
    evidence = {
        "seeds": [seed, seed + REPLAYS - 1],
        "tasks": len(tasks),
        "symptom": symptom,
        "head_failures": dict(head_failed),
        "base_failures": dict(base_failed),
        "profile": profile.name,
    }
    if symptom is None:
        clean = REPLAYS - len({r.seed for r in head_runs if not r.passed})
        verdict = passed if clean == REPLAYS else failed
        return verdict("G2", f"repro tasks pass {clean}/{REPLAYS} fresh seeds", **evidence)
    worse = {k: v for k, v in head_failed.items() if k != symptom and v > base_failed.get(k, 0)}
    if head_failed.get(symptom, 0) or worse:
        return failed(
            "G2",
            f"{symptom} on the head: {head_failed.get(symptom, 0)}; "
            f"more other failures than the base: {worse or 'none'}",
            **evidence,
        )
    return passed(
        "G2",
        f"no {symptom} on the head at {REPLAYS} fresh seeds (base: {base_failed.get(symptom, 0)})",
        **evidence,
    )


def run_g7(
    target: Target,
    base: str,
    head: str,
    *,
    seed: int,
    task_ids: list[str],
    identity: str | None,
    symptom: str | None = None,
) -> dict[str, Any]:
    results: dict[str, GateResult] = {}
    with clean_checkout(target.repo, base) as before, clean_checkout(target.repo, head) as after:
        results["G1"] = g1.build_static(target, after)
        results["G2"] = repro_fresh(target, before, after, task_ids, seed, symptom)
        results["G3"] = g3.pytest_flips(
            g3.pytest_results(target, before), g3.pytest_results(target, after)
        )
        if identity is None:
            results["G4"] = failed("G4", "no holdout identity (HOLDOUT_AGE_IDENTITY is not set)")
        else:
            seeds = list(range(seed, seed + REPLAYS))
            results["G4"] = g4.held_out_efficacy(target, before, after, identity, seeds=seeds)
    ok = all(r.passed for r in results.values())
    return {
        "host": target.id,
        "base": base,
        "head": head,
        "seed": seed,
        "passed": ok,
        "gates": {name: r.as_dict() for name, r in results.items()},
    }


def report(summary: dict[str, Any]) -> str:
    lines = [
        f"G7 for {summary['host']} at seed {summary['seed']}: "
        f"{'PASS' if summary['passed'] else 'FAIL'}"
    ]
    for name, gate in summary["gates"].items():
        lines.append(f"  {name} {gate['status'].upper():4} {gate['summary']}")
    return "\n".join(lines) + "\n" + json.dumps(summary, sort_keys=True) + "\n"
