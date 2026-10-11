"""G2, reproduction fixed (spec §5.11): the incident's cassette must pass 3/3 after the fix.

The same cassette G0 recorded is replayed against the candidate's checkout. A fix that makes
requests the cassette never saw fails the task (``CassetteMiss``), so G2 cannot be passed by
talking to the world instead of fixing the code. On failure the Surgeon gets the replay diff:
which recorded failing tasks still fail, and with which verdict (visible train tasks only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kintsugi.gates.common import GateResult, failed, passed
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target
from kintsugi.sandbox.cassette import Cassette

REPLAYS = 3


def reproduction_fixed(
    target: Target,
    checkout: Path,
    cassette: Cassette,
    tasks: list[dict[str, Any]],
    failing: list[str],
    *,
    seed: int,
    split: str,
    replays: int = REPLAYS,
) -> GateResult:
    still: dict[str, str] = {}
    clean = 0
    for _ in range(replays):
        verdicts = run_tasks(target, checkout, tasks, seed=seed, split=split, cassette=cassette,
                             phase="fixed").verdicts()  # fmt: skip
        bad = {t: verdicts.get(t, "MISSING") for t in failing if verdicts.get(t) != "PASS"}
        clean += not bad
        still.update(bad)
    evidence = {"replays_passing": clean, "replays": replays, "failing_tasks": len(failing),
                "cassette_sha": cassette.sha256}  # fmt: skip
    if clean == replays:
        return passed("G2", f"cassette passes {clean}/{replays}", **evidence)
    return failed("G2", f"cassette passes {clean}/{replays}", still_failing=still, **evidence)
