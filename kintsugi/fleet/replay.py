"""Record a host's run into one cassette and replay it (spec §5.11 G0/G2).

``record`` runs a host's tasks behind a live fabric whose chaos and LLM proxies both write into
one recorder, so the cassette holds every tool exchange (faults at their original positions)
and every model answer. ``replay`` runs the same tasks behind a fabric that serves only from
that cassette: no upstream is called, and a request the cassette never saw is an error, so the
verdicts are a pure function of the cassette and the host's code. Stage 6 builds the same
cassettes from neatlogs span payloads; G0 expects a replay to fail 3/3 before a fix and G2 to
pass 3/3 after it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kintsugi.sandbox.cassette import Cassette, Player, Recorder

from .baseline import run_eval
from .fabric import Fabric, FabricConfig
from .hosts import HostSpec


@dataclass(frozen=True)
class Recording:
    cassette: Cassette
    verdicts: dict[str, str]


def _verdicts(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {str(row["task"]): str(row["verdict"]) for row in rows}


def record(spec: HostSpec, *, split: str, seed: int, limit: int | None = None) -> Recording:
    recorder = Recorder(f"{spec.id}-{split}-seed{seed}", meta={"host": spec.id, "seed": seed})
    config = FabricConfig(seed=seed, chaos_recorder=recorder, llm_recorder=recorder)
    with Fabric({spec.id: spec}, config) as fabric:
        run = run_eval(fabric, spec, split, seed, limit=limit, workers=4)
    return Recording(recorder.cassette(), _verdicts(run.rows))


def replay(
    spec: HostSpec, cassette: Cassette, *, split: str, seed: int, limit: int | None = None
) -> dict[str, str]:
    """Verdicts from a cassette alone. ``seed`` is only a label here: nothing is drawn."""
    config = FabricConfig(seed=seed, player=Player(cassette))
    with Fabric({spec.id: spec}, config) as fabric:
        run = run_eval(fabric, spec, split, seed, limit=limit, workers=4)
    return _verdicts(run.rows)
