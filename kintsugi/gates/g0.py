"""G0, reproduction (spec §5.11): the cassette must fail 3/3 before any repair is attempted.

The failing tasks are re-run exactly as they ran in production: same task, same seed, same
chaos profile (faults are a pure function of the seed, amendment A-24), against the incident's
base commit, while one recorder captures every tool and model exchange. The resulting cassette
is then replayed three times against the same code with no upstream at all: a replay is a pure
function of the cassette and the code, so failing 3/3 shows the failure is in the code under
these inputs, not in the weather. No reproduction, no repair: the Pathologist widens the task
set ×3 once, then the incident is PARKED (I3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.gates.common import GateResult, failed, passed
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.sandbox.cassette import Cassette

REPLAYS = 3


@dataclass
class Reproduction:
    result: GateResult
    cassette: Cassette | None
    failing: list[str] = field(default_factory=list)  # tasks that failed in the recording
    replays: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.passed


def reproduce(
    target: Target,
    checkout: Path,
    tasks: list[dict[str, Any]],
    *,
    seed: int,
    split: str,
    profile: chaos.Profile | None = None,
    replays: int = REPLAYS,
) -> Reproduction:
    recording = run_tasks(target, checkout, tasks, seed=seed, split=split, profile=profile,
                          record=True, phase="repro")  # fmt: skip
    failing = recording.failed()
    cassette = recording.cassette
    if not failing or cassette is None:
        return Reproduction(failed("G0", f"0/{len(tasks)} tasks failed when re-run at seed {seed}",
                                   tasks=len(tasks), seed=seed), cassette)  # fmt: skip
    rounds = []
    for _ in range(replays):
        replay = run_tasks(target, checkout, tasks, seed=seed, split=split, cassette=cassette,
                           phase="repro")  # fmt: skip
        rounds.append(replay.verdicts())
    fails = sum(1 for verdicts in rounds if all(verdicts.get(t) != "PASS" for t in failing))
    evidence = {"seed": seed, "failing": len(failing), "tasks": len(tasks),
                "cassette_sha": cassette.sha256, "replays_failing": fails,
                "replays": replays}  # fmt: skip
    if fails == replays:
        result = passed("G0", f"cassette fails {fails}/{replays}", **evidence)
    else:
        result = failed("G0", f"cassette fails only {fails}/{replays}", **evidence)
    return Reproduction(result, cassette, failing, rounds)
