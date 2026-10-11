"""Running a host's tasks against one checkout, for a gate (G0, G2, G4, disputes, G7).

The host process runs the checkout's code (``Target.code_env``) behind a fresh fabric: the
seeded chaos proxy (or a cassette player) and the LLM metering proxy. Each task's row carries
its verdict, its wall-clock latency and its cost: the LLM spend the proxy metered for that task
plus the host's paid external calls (``economics.service_costs_usd``) counted in the chaos
proxy's log. A recording run also returns the cassette (tool and model I/O, faults at their
original positions); a replay run serves only from a cassette.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.fleet.baseline import run_eval
from kintsugi.fleet.fabric import Fabric, FabricConfig
from kintsugi.gates.target import Target
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.sandbox.cassette import Cassette, Player, Recorder
from kintsugi.sandbox.llm_proxy import account_of


@dataclass(frozen=True)
class TaskRun:
    task: str
    seed: int
    verdict: str
    latency_s: float
    cost_usd: float

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"


@dataclass
class EvalResult:
    runs: list[TaskRun]
    chaos_log: list[dict[str, Any]] = field(default_factory=list)
    cassette: Cassette | None = None
    model_mode: str = "offline"

    def verdicts(self) -> dict[str, str]:
        return {run.task: run.verdict for run in self.runs}

    def failed(self) -> list[str]:
        return sorted(run.task for run in self.runs if not run.passed)


def tasks_jsonl(tasks: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(task, sort_keys=True) + "\n" for task in tasks)


def run_tasks(
    target: Target,
    checkout: Path,
    tasks: list[dict[str, Any]],
    *,
    seed: int,
    split: str = "train",
    profile: chaos.Profile | None = None,
    record: bool = False,
    cassette: Cassette | None = None,
    workers: int = 4,
    phase: str = "gate",
    timeout_s: float = 1800.0,
) -> EvalResult:
    if record and cassette is not None:
        raise ValueError("record or replay, not both")
    target.make_importable()
    recorder = (Recorder(f"{target.id}-{split}-seed{seed}", meta={"host": target.id, "seed": seed})
                if record else None)  # fmt: skip
    config = FabricConfig(
        seed=seed,
        profile=profile or chaos.background_profile(),
        chaos_recorder=recorder,
        llm_recorder=recorder,
        player=Player(cassette) if cassette is not None else None,
        live_services=target.live_services,
    )
    with Fabric({target.id: target.host}, config) as fabric:
        run = run_eval(fabric, target.host, split, seed, tasks_jsonl=tasks_jsonl(tasks),
                       workers=workers, extra_env=target.code_env(checkout),
                       timeout_s=timeout_s, phase=phase)  # fmt: skip
        log = list(fabric.chaos.log)
        charges = fabric.spend.charges(account_of(target.id))
        mode = fabric.model_mode
    llm: dict[str, float] = {}
    for charge in charges:
        task = str(charge["task"])
        llm[task] = llm.get(task, 0.0) + float(charge["cost_usd"])
    prices = target.host.economics.service_costs_usd
    services: dict[str, float] = {}
    for row in log:
        task = str(row["task"])
        services[task] = services.get(task, 0.0) + prices.get(str(row["service"]), 0.0)
    runs = [TaskRun(str(row["task"]), seed, str(row["verdict"]), float(row["latency_s"]),
                    round(llm.get(str(row["task"]), 0.0) + services.get(str(row["task"]), 0.0), 8))
            for row in run.rows]  # fmt: skip
    return EvalResult(runs, log, recorder.cassette() if recorder is not None else None, mode)
