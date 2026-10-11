"""The Stage 4 detection rehearsal: R-007 fires on Ledger FAIL verdicts, end to end.

Ledger runs a few train tasks under the disclosed ``rehearsal-fx-outage`` profile (the FX API
answers 503), with spans exported and tagged ``phase:rehearsal``. Every task that needs a
conversion fails and its grader emits ``FAIL:<reason>``; the provisioned ``R-007 ledger``
detection matches those verdicts, its threshold rule fires and the webhook reaches the ingress,
and the poller claims the hits. No host code changes: the fault is environmental and disclosed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.fleet.baseline import run_eval
from kintsugi.fleet.fabric import Fabric, FabricConfig, load_dotenv
from kintsugi.fleet.hosts import load_hosts
from kintsugi.sandbox.chaos_proxy import PROFILES, load_profile
from kintsugi.senses.detections import Manifest
from kintsugi.senses.neatlogs_api import NeatlogsApi
from kintsugi.senses.poller import Poller, SweepReport

PROFILE = PROFILES / "rehearsal-fx-outage.yaml"


@dataclass
class RehearsalReport:
    rows: list[dict[str, Any]]
    detection_id: str
    hits: int = 0
    sweeps: list[SweepReport] = field(default_factory=list)
    waited_s: float = 0.0

    @property
    def failed_tasks(self) -> list[str]:
        return [r["task"] for r in self.rows if str(r["verdict"]).startswith("FAIL:")]


def r007_detection(manifest: Manifest, host: str = "ledger") -> str:
    for detection_id, binding in manifest.detections.items():
        if binding.receptor == "R-007" and binding.host == host:
            return detection_id
    raise LookupError(f"R-007 {host} is not provisioned: run `kintsugi senses provision`")


def rehearse(
    repo: Path,
    api: NeatlogsApi,
    manifest: Manifest,
    poller: Poller,
    *,
    tasks: int = 6,
    seed: int = 11,
    timeout_s: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
) -> RehearsalReport:
    env = load_dotenv(repo / ".env")
    if "NEATLOGS_API_KEY" not in env:
        raise RuntimeError("the rehearsal exports spans: NEATLOGS_API_KEY must be in .env")
    spec = load_hosts(repo)["ledger"]
    profile = load_profile(PROFILE).with_seed(seed)
    with Fabric({"ledger": spec}, FabricConfig(seed=seed, profile=profile)) as fabric:
        run = run_eval(fabric, spec, "train", seed, limit=tasks, workers=2, phase="rehearsal",
                       extra_env={"NEATLOGS_API_KEY": env["NEATLOGS_API_KEY"]})  # fmt: skip
    report = RehearsalReport(run.rows, r007_detection(manifest))
    wanted = {f"task:{task}" for task in report.failed_tasks}
    mine = {f"seed:{seed}", "phase:rehearsal"}  # task names repeat across runs; seeds do not
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        sweep = poller.sweep(report.detection_id)
        report.sweeps.append(sweep)
        names = set()
        for hit, _ in _recent(api, report.detection_id):
            trace = api.trace(hit.trace_id)
            if mine <= set(trace.tags):
                names.add(trace.trace_name)
        report.hits = len(wanted & names)
        if wanted and report.hits >= len(wanted):
            break
        sleep(15.0)
    report.waited_s = time.monotonic() - started
    return report


def _recent(api: NeatlogsApi, detection_id: str, limit: int = 50) -> list[tuple[Any, Any]]:
    out: list[tuple[Any, Any]] = []
    for hits, page in api.hit_pages(detection_id, limit=limit):
        out.extend((hit, page) for hit in hits)
        break
    return out
