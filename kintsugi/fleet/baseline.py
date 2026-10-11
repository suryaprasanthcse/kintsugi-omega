"""Baselines and health cards (spec §5.9.4 O6, §6.1 precondition 1).

For each seed a fresh fabric starts (the chaos proxy's faults depend on the seed), the host's
eval runner runs as a child process through the subprocess gateway, and its verdict rows are
pooled into a health card: pass rate with a Wilson interval per seed and pooled, p95 latency,
metered cost, the verdict histogram and the chaos the host absorbed. The holdout is decrypted
here and piped to the child on stdin; the key never reaches the host process.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from kintsugi.sandbox import proc
from kintsugi.sandbox.llm_proxy import account_of
from kintsugi.stats.wilson import wilson_interval

from .fabric import Fabric, FabricConfig
from .hosts import HostSpec


@dataclass(frozen=True)
class EvalRun:
    host: str
    split: str
    seed: int
    rows: list[dict[str, Any]]
    duration_s: float


class EvalFailed(RuntimeError):
    pass


def run_eval(
    fabric: Fabric,
    spec: HostSpec,
    split: str,
    seed: int,
    *,
    tasks_jsonl: str | None = None,
    workers: int = 8,
    limit: int | None = None,
    extra_env: Mapping[str, str] | None = None,
    timeout_s: float = 3600.0,
    phase: str | None = None,
) -> EvalRun:
    argv = [sys.executable, "-m", "hostkit.evalrun", "--host", spec.package, "--split", split,
            "--seed", str(seed), "--workers", str(workers)]  # fmt: skip
    if phase is not None:
        argv += ["--phase", phase]
    if tasks_jsonl is not None:
        argv += ["--tasks-file", "-"]
    if limit is not None:
        argv += ["--limit", str(limit)]
    env = {**fabric.env(spec.id), **(extra_env or {})}
    result = proc.run(argv, extra_env=env, input_text=tasks_jsonl, timeout_s=timeout_s)
    if result.returncode != 0:
        raise EvalFailed(f"{spec.id} {split} seed={seed}: {result.stderr[-2000:]}")
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    return EvalRun(spec.id, split, seed, rows, result.duration_s)


def _rate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    passed = sum(row["verdict"] == "PASS" for row in rows)
    low, high = wilson_interval(passed, n) if n else (0.0, 1.0)
    return {"n": n, "passed": passed, "pass_rate": round(passed / n, 4) if n else None,
            "wilson95": [round(low, 4), round(high, 4)]}  # fmt: skip


def health_card(
    spec: HostSpec,
    runs: Sequence[EvalRun],
    *,
    model_mode: str,
    metered_usd: float,
    billable_usd: float,
    chaos: Mapping[str, int],
    chaos_profile: str,
) -> dict[str, Any]:
    rows = [row for run in runs for row in run.rows]
    pooled = _rate(rows)
    latencies = [float(row["latency_s"]) for row in rows]
    p95 = float(np.percentile(latencies, 95)) if latencies else None
    meets_rate = pooled["pass_rate"] is not None and pooled["pass_rate"] >= spec.slo.pass_rate
    meets_p95 = spec.slo.p95_s is None or (p95 is not None and p95 <= spec.slo.p95_s)
    return {
        "host": spec.id,
        "model": spec.model,
        "model_mode": model_mode,
        "chaos_profile": chaos_profile,
        "seeds": sorted({run.seed for run in runs}),
        "splits": sorted({run.split for run in runs}),
        "per_seed": {
            str(seed): _rate([r for run in runs if run.seed == seed for r in run.rows])
            for seed in sorted({run.seed for run in runs})
        },
        "per_split": {
            split: _rate([r for run in runs if run.split == split for r in run.rows])
            for split in sorted({run.split for run in runs})
        },
        "pooled": pooled,
        "p95_latency_s": round(p95, 3) if p95 is not None else None,
        "slo": spec.slo.model_dump(),
        "meets_slo": bool(meets_rate and meets_p95),
        "verdicts": dict(Counter(row["verdict"] for row in rows).most_common()),
        "cost": {
            "metered_usd": round(metered_usd, 6),
            "billable_usd": round(billable_usd, 6),
            "per_task_usd": round(metered_usd / len(rows), 8) if rows else None,
        },
        "chaos": dict(sorted(chaos.items())),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def baseline(
    spec: HostSpec,
    seeds: Sequence[int],
    *,
    holdout_jsonl: str | None,
    config: FabricConfig | None = None,
    workers: int = 8,
    limit: int | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    runs: list[EvalRun] = []
    chaos: Counter[str] = Counter()
    metered = billable = 0.0
    mode, profile = "offline", "background"
    for seed in seeds:
        seeded = FabricConfig(seed=seed) if config is None else _with_seed(config, seed)
        with Fabric({spec.id: spec}, seeded) as fabric:
            mode, profile = fabric.model_mode, seeded.profile.name
            splits: list[tuple[str, str | None]] = [("train", None)]
            if holdout_jsonl is not None:
                splits.append(("holdout", holdout_jsonl))
            for split, tasks_jsonl in splits:
                run = run_eval(fabric, spec, split, seed, tasks_jsonl=tasks_jsonl,
                               workers=workers, limit=limit, extra_env=extra_env)  # fmt: skip
                runs.append(run)
            chaos.update(fabric.chaos_counts())
            account = account_of(spec.id)
            metered += fabric.spend.spent(account=account, billable=None)
            billable += fabric.spend.spent(account=account, billable=True)
    return health_card(spec, runs, model_mode=mode, metered_usd=metered, billable_usd=billable,
                       chaos=chaos, chaos_profile=profile)  # fmt: skip


def _with_seed(config: FabricConfig, seed: int) -> FabricConfig:
    return FabricConfig(seed=seed, profile=config.profile, daily_cap_usd=config.daily_cap_usd,
                        spend_path=config.spend_path, anthropic_key=config.anthropic_key,
                        bind=config.bind)  # fmt: skip
