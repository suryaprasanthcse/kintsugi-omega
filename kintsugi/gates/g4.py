"""G4, held-out efficacy (spec §5.11, §5.12; invariant I5).

The sealed holdout is decrypted **here and only here** (the identity is passed into this
function by the Auditor's G4 activity and never stored), every holdout task runs at the base
commit and at the candidate for each seed, and the pairs (task × seed) are compared:

- exact one-sided McNemar on the discordant pairs (b: fail→pass, c: pass→fail), p < α;
- cost per task: the one-sided 95% bootstrap upper bound of the relative change < +5%;
- p95 latency: the same bound on the relative change of the 95th percentile < +10%.

Only aggregates leave: pair counts, b, c, p, pass rates with Wilson intervals and the two
bounds. Task ids, inputs and outputs never do, and the result is checked for the holdout's
canary before it is returned (an I5 leak fails the gate loudly instead of shipping).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from kintsugi.gates import holdout as custody
from kintsugi.gates.common import GateResult, failed, passed
from kintsugi.gates.evals import EvalResult, TaskRun, run_tasks
from kintsugi.gates.target import Target
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.stats.bootstrap import COST_MARGIN, P95_MARGIN, relative_delta
from kintsugi.stats.mcnemar import mcnemar
from kintsugi.stats.wilson import wilson_interval

ALPHA = 0.05


class HoldoutLeak(RuntimeError):
    """Holdout content reached a G4 output: the gate refuses to return it (I5)."""


@dataclass(frozen=True)
class Efficacy:
    n_pairs: int
    b: int
    c: int
    p: float
    pass_before: float
    pass_after: float
    ci_before: tuple[float, float]
    ci_after: tuple[float, float]
    cost_before: float
    cost_after: float
    cost_delta: float | None
    cost_ub: float | None
    p95_before: float
    p95_after: float
    p95_delta: float
    p95_ub: float
    seeds: tuple[int, ...]

    def aggregate(self) -> dict[str, Any]:
        """The only form G4 results leave the gate in (Surgeons see Δpass, CI and cost Δ)."""

        def r(x: float | None) -> float | None:
            return None if x is None else round(x, 6)

        return {"n_pairs": self.n_pairs, "b": self.b, "c": self.c, "p": self.p,
                "pass_before": r(self.pass_before), "pass_after": r(self.pass_after),
                "delta_pass": r(self.pass_after - self.pass_before),
                "ci_before": [r(x) for x in self.ci_before],
                "ci_after": [r(x) for x in self.ci_after],
                "cost_before": r(self.cost_before), "cost_after": r(self.cost_after),
                "cost_delta": r(self.cost_delta), "cost_ub": r(self.cost_ub),
                "p95_before": r(self.p95_before), "p95_after": r(self.p95_after),
                "p95_delta": r(self.p95_delta), "p95_ub": r(self.p95_ub),
                "seeds": list(self.seeds)}  # fmt: skip


def efficacy(before: Sequence[TaskRun], after: Sequence[TaskRun], seed: int = 0) -> Efficacy:
    """Paired statistics over runs matched by (task, seed)."""
    a_index = {(run.task, run.seed): run for run in after}
    pairs = [(run, a_index[(run.task, run.seed)]) for run in before
             if (run.task, run.seed) in a_index]  # fmt: skip
    if not pairs:
        raise ValueError("no paired runs")
    pb = [b.passed for b, _ in pairs]
    pa = [a.passed for _, a in pairs]
    test = mcnemar(pb, pa)
    n = len(pairs)
    cost_b, cost_a = [b.cost_usd for b, _ in pairs], [a.cost_usd for _, a in pairs]
    lat_b, lat_a = [b.latency_s for b, _ in pairs], [a.latency_s for _, a in pairs]
    cost = (relative_delta(cost_b, cost_a, statistic="mean", seed=seed)
            if sum(cost_b) > 0 else None)  # fmt: skip
    p95 = relative_delta(lat_b, lat_a, statistic="p95", seed=seed)
    return Efficacy(
        n_pairs=n, b=test.b, c=test.c, p=test.p,
        pass_before=sum(pb) / n, pass_after=sum(pa) / n,
        ci_before=wilson_interval(sum(pb), n), ci_after=wilson_interval(sum(pa), n),
        cost_before=float(np.mean(cost_b)), cost_after=float(np.mean(cost_a)),
        cost_delta=cost.point if cost else None, cost_ub=cost.upper if cost else None,
        p95_before=float(np.percentile(lat_b, 95)), p95_after=float(np.percentile(lat_a, 95)),
        p95_delta=p95.point, p95_ub=p95.upper,
        seeds=tuple(sorted({b.seed for b, _ in pairs})),
    )  # fmt: skip


def verdict(stats: Efficacy, *, alpha: float = ALPHA) -> GateResult:
    reasons = []
    if not stats.p < alpha:
        reasons.append(f"McNemar p = {stats.p:.3g} (needs < {alpha})")
    if stats.cost_ub is not None and not stats.cost_ub < COST_MARGIN:
        reasons.append(f"cost Δ upper bound {stats.cost_ub:+.1%} (needs < +{COST_MARGIN:.0%})")
    if not stats.p95_ub < P95_MARGIN:
        reasons.append(f"p95 Δ upper bound {stats.p95_ub:+.1%} (needs < +{P95_MARGIN:.0%})")
    evidence = stats.aggregate()
    if reasons:
        return failed("G4", "; ".join(reasons), **evidence)
    return passed("G4", f"b={stats.b}, c={stats.c}, p={stats.p:.3g}; bounds hold", **evidence)


def check_no_leak(result: GateResult, canary: str, task_ids: Sequence[str]) -> GateResult:
    blob = json.dumps(result.as_dict())
    if custody.canary_hits(blob.encode("utf-8"), canary) or any(t in blob for t in task_ids):
        raise HoldoutLeak("a G4 result contains holdout content")
    return result


Runner = Callable[[Path, list[dict[str, Any]], int], EvalResult]


def held_out_efficacy(
    target: Target,
    base_checkout: Path,
    head_checkout: Path,
    identity: str,
    *,
    seeds: Sequence[int],
    profile: chaos.Profile | None = None,
    base_cache: dict[tuple[str, int], EvalResult] | None = None,
    base_key: str = "",
) -> GateResult:
    if target.holdout is None:
        return failed("G4", f"{target.id} declares no sealed holdout")
    payload = custody.open_holdout(target.repo / target.holdout, identity)
    tasks: list[dict[str, Any]] = list(payload["tasks"])
    task_ids = [str(task["id"]) for task in tasks]
    before: list[TaskRun] = []
    after: list[TaskRun] = []
    for seed in seeds:
        cached = base_cache.get((base_key, seed)) if base_cache is not None else None
        base = cached or run_tasks(target, base_checkout, tasks, seed=seed, split="holdout",
                                   profile=profile, phase="g4-before")  # fmt: skip
        if base_cache is not None:
            base_cache[(base_key, seed)] = base
        head = run_tasks(target, head_checkout, tasks, seed=seed, split="holdout",
                         profile=profile, phase="g4-after")  # fmt: skip
        before += base.runs
        after += head.runs
    result = verdict(efficacy(before, after))
    return check_no_leak(result, str(payload["canary"]), task_ids)
