"""The primary incident loop end to end (spec §5.13, §6.2–§6.4): the Incident workflow on
Temporal's time-skipping server, the real crew and every real gate on the Ledger practice
repository; only the models and the Surgeons' edits are scripted. Round 0 fails and is rewound
(L1), round 1 ends in a pre-registered dispute, the winner ships and the canary accepts. The L4
drill moves main mid-round and checks the winner is re-verified on the new main."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW, SENSES, SURGEON, OmegaActivities
from kintsugi.control.workflows import WORKFLOWS, Incident
from kintsugi.crew import claude_session as cs
from kintsugi.crew.context import CrewContext, LocalTraces
from kintsugi.crew.factory import OmegaCrew, Shipper
from kintsugi.crew.llm import ScriptedModel
from kintsugi.crew.surgeon import ClaudeSurgeon
from kintsugi.gates import holdout
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import load_target
from kintsugi.proof import attest
from kintsugi.senses.signature import from_chaos_log, tool_names
from tests.helpers import practice as pr

HEALTHY = (pr.FIXTURE / pr.RECONCILE).read_text(encoding="utf-8")
CONFIG = c.ControlConfig(canary_interval_s=1.0, canary_max_batches=6)


def naive_hoist(text: str) -> str:
    """Hoists the session but restarts every attempt from the first line: no duplicates, but
    the resume cursor is lost (spec §6.3 H2: G3 flips test_resume_after_partial_batch)."""
    text = text.replace(pr.NESTED, pr.HOISTED)
    return text.replace(
        "        for line in current.pending():",
        "        current.cursor = 0  # start the pass over\n        for line in current.pending():",
    )


def hoist_b(text: str) -> str:
    return text.replace(
        pr.NESTED,
        pr.HOISTED.replace(
            "outside the retried scope", "one session per call, kept across retries"
        ),
    )


EDITS: dict[str, Callable[[Path], None]] = {
    "revert the refactor": lambda wt: pr.write(wt, pr.RECONCILE, HEALTHY),
    "hoist the session": lambda wt: pr.write(
        wt, pr.RECONCILE, naive_hoist((wt / pr.RECONCILE).read_text("utf-8"))
    ),
    "idempotent booking": lambda wt: _lookup(wt),
    "hoist and keep the cursor": lambda wt: pr.write(
        wt, pr.RECONCILE, (wt / pr.RECONCILE).read_text("utf-8").replace(pr.NESTED, pr.HOISTED)
    ),
    "hoist under a clearer name": lambda wt: pr.write(
        wt, pr.RECONCILE, hoist_b((wt / pr.RECONCILE).read_text("utf-8"))
    ),
}


def _lookup(wt: Path) -> None:
    text = (wt / pr.RECONCILE).read_text("utf-8")
    text = text.replace(
        "        if line.key not in self.booked:\n", pr.LOOKUP.split("\n")[1] + "\n"
    )
    text = text.replace(
        "from practice_ledger.tools import fx_rate, post_line",
        "from practice_ledger.tools import already_booked, fx_rate, post_line",
    )
    pr.write(wt, pr.RECONCILE, text)
    tools = (wt / "practice_ledger/tools.py").read_text("utf-8")
    pr.write(
        wt,
        "practice_ledger/tools.py",
        tools
        + (
            '\n\n@span(kind="TOOL", name="already_booked")\n'
            "def already_booked(key: str) -> bool:\n"
            '    return external.call("books", "GET", f"/lines/{key}").status_code == 200\n'
        ),
    )


def card(title: str, files: list[str] | None = None) -> dict[str, Any]:
    return {
        "title": title,
        "root_cause": "the posting session is rebuilt on every retry",
        "approach": title,
        "intent_constraints": [],
        "diff_budget": 40,
        "allowed_files": files or [pr.RECONCILE],
    }


def coordinator_cards(call: Any) -> dict[str, Any]:
    found = re.search(r", round (\d+)\. Write exactly", call.prompt)
    assert found is not None
    if found.group(1) == "0":
        return {
            "cards": [
                card("revert the refactor"),
                card("hoist the session"),
                card("idempotent booking", [pr.RECONCILE, "practice_ledger/tools.py"]),
            ]
        }
    return {"cards": [card("hoist and keep the cursor"), card("hoist under a clearer name")]}


class FakeSession:
    """Applies the card's scripted edit, reporting it as Edit tool calls (A-16)."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.started = threading.Event()
        self.prompts: list[str] = []

    def __call__(self, request: cs.SessionRequest, on_start: Any = None) -> cs.SessionResult:
        if on_start is not None:
            on_start(4242)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=120)
        self.prompts.append(request.prompt)
        title = re.search(r"^Hypothesis: (.+)$", request.prompt, re.MULTILINE)
        assert title is not None
        before = {
            p: (Path(request.cwd) / p).read_bytes()
            for p in (pr.RECONCILE, "practice_ledger/tools.py")
        }
        EDITS[title.group(1)](Path(request.cwd))
        changed = [p for p, b in before.items() if (Path(request.cwd) / p).read_bytes() != b]
        stream = cs.StreamSummary(session_id=request.session_id)
        stream.tool_uses = [
            {"name": "Edit", "input": {"file_path": str(Path(request.cwd) / p)}} for p in changed
        ]
        stream.result = {"result": f"applied {title.group(1)}", "total_cost_usd": 0.4}
        return cs.SessionResult(request, stream, 0, False, False, None, None)


class LocalMerge(Shipper):
    """Merges the winner's branch into main with a merge commit (the attested commit stays)."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def ship(self, out: c.SurgeonOutcome) -> c.Shipment:
        pr.git(
            self.repo,
            *pr.IDENTITY,
            "merge",
            "--no-ff",
            "-q",
            "-m",
            f"Merge {out.branch} ({out.incident_id})",
            out.branch,
        )
        sha = pr.git(self.repo, "rev-parse", "HEAD")
        return c.Shipment(
            out.incident_id, out.hypothesis, out.branch, f"local://merge/{sha}", True, True, sha
        )


def build(root: Path, session: FakeSession) -> tuple[OmegaCrew, CrewContext, c.Alert, str]:
    p = pr.make_repo(root / "repo")
    buggy = pr.plant_refactor(p.repo)
    (root / "holdout.agekey").write_text(p.identity + "\n", encoding="ascii")
    target = load_target(p.repo, pr.HOST)
    train = importlib.import_module("practice_ledger.evals.generate").tasks("train")
    with clean_checkout(p.repo, buggy) as checkout:
        live = run_tasks(target, checkout, train, seed=1, phase="live")
    observations = from_chaos_log(
        live.chaos_log,
        live.verdicts(),
        host=pr.HOST,
        split="train",
        seed=1,
        run="live1",
        phase="live",
        services=tool_names(target.host.tools),
        detection_ids=["omega:R-007:ledger-practice:v1"],
    )
    LocalTraces.write(root / "obs", "live1", observations)
    ctx = CrewContext(
        state=root / "crew",
        targets={pr.HOST: target},
        model=ScriptedModel({"coordinator": coordinator_cards}),
        traces=LocalTraces(root / "obs"),
        holdout_identity=root / "holdout.agekey",
        g4_seeds=(1, 2),  # 80 pairs: the p95 bound is stable enough for equivalent fixes
    )
    surgeon = ClaudeSurgeon(
        ctx, attest.Ed25519Signer.generate(), run=session, require_checkpoint=False
    )
    crew = OmegaCrew(ctx, runner=lambda: surgeon, shipper=LocalMerge(p.repo))
    hits = tuple(
        c.Hit(
            f"k{i}", "omega:R-007:ledger-practice:v1", o.trace_id, "s", "poller", "R-007", pr.HOST
        )
        for i, o in enumerate(observations)
    )
    return crew, ctx, c.Alert("INC-0042", pr.HOST, hits, "high"), p.identity


async def run_incident(crew: OmegaCrew, alert: c.Alert, during: Any = None) -> c.Outcome:
    env = await WorkflowEnvironment.start_time_skipping()
    acts = OmegaActivities(crew, heartbeat_interval_s=0.2)
    try:
        async with (
            Worker(
                env.client,
                task_queue=CREW,
                workflows=list(WORKFLOWS),
                activities=acts.activities_for(CREW),
            ),
            Worker(
                env.client,
                task_queue=SURGEON,
                activities=acts.activities_for(SURGEON),
                max_concurrent_activities=3,
            ),
            Worker(env.client, task_queue=SENSES, activities=acts.activities_for(SENSES)),
        ):
            handle = await env.client.start_workflow(
                Incident.run, args=[alert, CONFIG], id=alert.incident_id, task_queue=CREW
            )
            if during is not None:
                await during(handle)
            return await handle.result()
    finally:
        await env.shutdown()


def test_round_one_fails_round_two_disputes_then_ships(tmp_path: Path) -> None:
    crew, ctx, alert, identity = build(tmp_path, FakeSession())
    outcome = asyncio.run(run_incident(crew, alert))
    assert outcome.state == "RESOLVED", outcome.reason
    states = [t.target for t in outcome.transitions]
    assert states.count("REPAIRING") == 2  # round 0 failed, round 1 repaired
    assert (states[0], states[-1]) == ("REPRODUCED", "RESOLVED")
    view = ctx.case("INC-0042").reader("coordinator")
    verdicts = {h: view.get("verdict", h) for h in ("H1", "H2", "H3", "H4", "H5")}
    assert verdicts["H1"]["gates"]["G5"]["status"] == "warn"  # the revert: a fallback only
    assert verdicts["H2"]["gates"]["G3"]["status"] == "fail"  # the naive hoist lost the cursor
    assert not verdicts["H3"]["passed"]  # the lookup breaks visible tests
    assert verdicts["H4"]["passed"]
    assert verdicts["H5"]["passed"]
    decision = view.get("decision", "round1")
    assert decision["dispute"].startswith("sha256:")
    result = view.get("experiment", decision["dispute"].removeprefix("sha256:")[:16] + "-result")
    assert outcome.shipment is not None
    assert result["winner"] == outcome.shipment.hypothesis
    repo = ctx.targets[pr.HOST].repo
    pinned = pr.git(repo, "for-each-ref", "--format=%(refname)", "refs/kintsugi/attempts/INC-0042")
    assert {"H1", "H2", "H3"} <= {ref.split("/")[4] for ref in pinned.split()}  # L1 evidence
    merge = pr.git(repo, "rev-parse", "main")
    assert (
        pr.git(repo, "rev-parse", f"{merge}^2")
        == view.get("verdict", outcome.shipment.hypothesis)["commit"]
    )
    payload = holdout.open_holdout(repo / ctx.targets[pr.HOST].holdout, identity)  # type: ignore[operator]
    canary = str(payload["canary"])
    case_root = ctx.state / "cases" / "INC-0042"
    blob = b"".join(p.read_bytes() for p in case_root.rglob("*") if p.is_file())
    assert holdout.canary_hits(blob, canary) == 0  # I5: zero holdout leaks in the case file
    model = ctx.model
    assert isinstance(model, ScriptedModel)
    assert all(holdout.canary_hits(call.prompt.encode(), canary) == 0 for call in model.calls)
    auditor_reads = {a["kind"] for a in ctx.case("INC-0042").accesses() if a["role"] == "auditor"}
    assert not auditor_reads & {"surgeon_report", "surgeon_session"}
    ledger = crew.ledger.entries()
    assert [e["type"] for e in ledger].count("rewind") >= 3
    assert json.dumps(ledger)


def test_l4_drift_mid_round_reverifies_the_winner_on_the_new_main(tmp_path: Path) -> None:
    gate = threading.Event()
    session = FakeSession(gate)
    crew, ctx, alert, _ = build(tmp_path, session)
    ctx.model = ScriptedModel({"coordinator": {"cards": [card("hoist and keep the cursor")]}})
    repo = ctx.targets[pr.HOST].repo

    async def drift(handle: Any) -> None:
        while not session.started.is_set():  # the round is running: move main under it
            await asyncio.sleep(0.2)
        agent = repo / "practice_ledger" / "agent.py"
        pr.write(
            repo,
            "practice_ledger/agent.py",
            agent.read_text("utf-8").replace("one document in", "one vendor document in"),
        )
        new_main = pr.commit_all(repo, "docs(agent): benign upstream drift (S-19 stand-in)")
        await handle.signal(Incident.drift_signal, args=["ledger-practice", new_main])
        gate.set()

    outcome = asyncio.run(run_incident(crew, alert, drift))
    assert outcome.state == "RESOLVED", outcome.reason
    record = ctx.case("INC-0042").reader("auditor").get("reverify", "H1")
    assert record["G1"] == "rerun"
    assert record["G2"] == "rerun"
    assert record["G7"] == "final-head"
    verdict = ctx.case("INC-0042").reader("coordinator").get("verdict", "H1")
    assert verdict["reverify"]["new_main"] == record["new_main"]
    assert outcome.shipment is not None
    assert outcome.shipment.merged
