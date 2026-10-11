"""The real crew behind the control plane's activities (Stage 5's ``Crew`` port).

``OmegaCrew.handle`` dispatches each activity to its role: Sentinel (``route_hit``), Pathologist
(``triage``, ``reproduce``), Archivist (``attribute``), governor and Coordinator
(``plan_budget``, ``hypothesize``, ``coordinate``, ``dispute_experiment``, ``budget_exhausted``),
Surgeons (``surgeon`` through Stage 5's ``SurgeonStep``), the Auditor (``audit_gates``), the
shipper (``ship``) and the L4 re-verification (``rebase_and_rerun``). Ledger-shaped steps
(``record_transition``, ``park``, ``escalate``, ``ledger_append``) append to a hash-chained
ledger file (``kintsugi.proof.ledger`` format) until Stage 8 moves it to the ``ledger`` branch.

Stage 7 owns antibodies, motifs, vaccination and quarantine: those steps answer honestly that
nothing exists yet (no antibody match, an empty motif scan, a placeholder antibody reference
recorded as pending). The canary is real: each batch runs the host's tasks on the merged code
under the background chaos profile.

Workers load it through ``kintsugi.crew.runtime`` (configured per deployment or rehearsal).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.control.activities import StepContext
from kintsugi.control.surgeon_step import AttemptLog, SessionRunner, SurgeonStep
from kintsugi.crew.archivist import Archivist
from kintsugi.crew.auditor import Auditor
from kintsugi.crew.context import CrewContext
from kintsugi.crew.coordinator import Coordinator
from kintsugi.crew.pathologist import Pathologist
from kintsugi.crew.sentinel import Sentinel
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.memory.graph import GraphError
from kintsugi.proof.ledger import Ledger, genesis_hash
from kintsugi.sandbox import worktree
from kintsugi.senses.drift import reverify_plan
from kintsugi.senses.fixes_mcp import Fixes, FixesUnavailable

CANARY_TASKS = 10


class FileLedger:
    """The hash-chained ledger in a JSONL file, appended under a process lock."""

    def __init__(self, path: Path, genesis: str) -> None:
        self.path = Path(path)
        self.genesis = genesis
        self._lock = threading.Lock()

    def append(self, entry: Mapping[str, Any]) -> str:
        with self._lock:
            text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            ledger = Ledger.from_jsonl(text, self.genesis)
            record = ledger.append(
                {"at": datetime.now(UTC).isoformat(timespec="seconds"), **dict(entry)}
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(ledger.to_jsonl(), encoding="utf-8", newline="\n")
            return str(record["hash"])

    def entries(self) -> tuple[dict[str, Any], ...]:
        text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        return Ledger.from_jsonl(text, self.genesis).entries


class Shipper:
    """``ship`` (``kintsugi.control.ship``): issue → PR with the receipt → G7 → merge."""

    def ship(self, out: c.SurgeonOutcome) -> c.Shipment:  # pragma: no cover - protocol
        raise NotImplementedError


class OmegaCrew:
    def __init__(
        self,
        ctx: CrewContext,
        *,
        runner: Callable[[], SessionRunner],
        shipper: Shipper | None = None,
        fixes: Fixes | None = None,
        tags: Any = None,  # kintsugi.senses.tags.TagWriter for neatlogs traces
        ledger: FileLedger | None = None,
    ) -> None:
        self.ctx = ctx
        self.runner = runner
        self.shipper = shipper
        self.fixes = fixes
        self.tags = tags
        self.ledger = ledger or FileLedger(
            ctx.state / "ledger.jsonl", genesis_hash("rehearsal", b"\0" * 32)
        )
        self.auditor = Auditor(ctx)
        self.coordinator = Coordinator(ctx)

    def handle(self, ctx: StepContext, *args: Any) -> Any:
        handler = getattr(self, f"do_{ctx.name}", None)
        if handler is None:
            raise NotImplementedError(f"the crew has no {ctx.name} step")
        return handler(ctx, *args)

    # ---- senses and diagnosis ------------------------------------------------------------------

    def do_route_hit(self, step: StepContext, hit: c.Hit, open_incidents: list[str]) -> Any:
        return Sentinel(self.ctx).route(hit, open_incidents)

    def do_triage(self, step: StepContext, alert: c.Alert) -> c.Signature:
        return Pathologist(self.ctx).triage(alert)

    def do_tag_traces(self, step: StepContext, incident: str, ids: list[str], phase: str) -> int:
        real = [t for t in ids if not t.startswith("local:")]
        if self.tags is None or not real:
            return 0
        for trace in real:
            self.tags.apply(trace, add=[f"incident:{incident}", f"phase:{phase}"])
        return len(real)

    def do_match_antibody(self, step: StepContext, sig: c.Signature) -> None:
        return None  # the antibody library is Stage 7: every incident is a primary response

    def do_reproduce(self, step: StepContext, sig: c.Signature) -> c.Repro:
        return Pathologist(self.ctx).reproduce(sig)

    def do_attribute(self, step: StepContext, sig: c.Signature, repro: c.Repro) -> c.Cause:
        return Archivist(self.ctx).attribute(sig, repro)

    def do_fixes_prior(self, step: StepContext, sig: c.Signature) -> str | None:
        if self.fixes is None:
            return None
        try:
            suggestions = self.fixes.triage_list(trace_ids=list(sig.trace_ids)[:20])
            if not suggestions:
                return None
            found = self.fixes.triage_get_investigation(str(suggestions[0]["id"]))
        except FixesUnavailable:
            return None
        return json.dumps(found)[:2000]

    # ---- planning and decisions -----------------------------------------------------------------

    def do_plan_budget(self, step: StepContext, sig: c.Signature, cause: c.Cause) -> Any:
        return self.coordinator.plan_budget(sig, cause)

    def do_hypothesize(self, step: StepContext, *args: Any) -> list[c.Card]:
        return self.coordinator.hypothesize(*args)

    def do_coordinate(self, step: StepContext, *args: Any) -> c.Decision:
        return self.coordinator.coordinate(*args)

    def do_dispute_experiment(self, step: StepContext, incident: str, d: c.Decision) -> Any:
        return self.coordinator.dispute_experiment(incident, d)

    def do_budget_exhausted(self, step: StepContext, *args: Any) -> bool:
        return self.coordinator.budget_exhausted(*args)

    # ---- repair -------------------------------------------------------------------------------

    def do_surgeon(self, step: StepContext, card: c.Card) -> c.SurgeonOutcome:
        brief = json.loads(card.brief)
        target = self.ctx.target(str(brief["host"]))
        case = self.ctx.case(card.incident_id)
        plan = case.reader("governor").get("plan", "plan", None) or {}
        budget = float(plan.get("budget_usd", 0.0))
        if plan and case.spent() >= budget:  # G6: a round never starts past the budget
            case.put(
                "refusal",
                card.hypothesis,
                {"reason": f"budget spent: ${case.spent():.2f} of ${budget:.2f} (G6)"},
                by="surgeon",
            )
            return c.SurgeonOutcome(
                card.incident_id,
                card.hypothesis,
                step.attempt,
                worktree.branch_name(card.incident_id, card.hypothesis),
                "",
                None,
            )
        attempts = AttemptLog(self.ctx.state / "attempts.sqlite")
        return SurgeonStep(target.repo, attempts, self.runner()).run(card, step)

    def do_audit_gates(self, step: StepContext, out: c.SurgeonOutcome) -> c.GateVerdict:
        return self.auditor.audit(out)

    def do_rewind(self, step: StepContext, outs: list[c.SurgeonOutcome]) -> None:
        """Discard the worktrees, keep each attempt's commit (and checkpoint) as evidence."""
        for out in outs:
            host = self._host(out)
            try:
                wt = worktree.open_existing(
                    self.ctx.target(host).repo, out.incident_id, out.hypothesis
                )
            except worktree.WorktreeError:
                continue  # already rewound (a retried step)
            kept = worktree.discard(wt)
            self.ledger.append(
                {
                    "type": "rewind",
                    "incident": out.incident_id,
                    "hypothesis": out.hypothesis,
                    "pinned": kept.pinned_ref,
                    "head": kept.head,
                }
            )

    def do_rebase_and_rerun(
        self, step: StepContext, outs: list[c.SurgeonOutcome], repo: str, new_main: str
    ) -> None:
        """L4: record, per candidate, what the move of main forces the gates to re-check."""
        for out in outs:
            if out.commit_sha is None:
                continue
            case = self.ctx.case(out.incident_id)
            card = case.reader("auditor").get("card", out.hypothesis)
            target = self.ctx.target(str(card["host"]))
            previous = case.reader("auditor").get("verdict", out.hypothesis, None) or {}
            try:
                plan = reverify_plan(target.repo, str(card["base_sha"]), new_main, out.commit_sha)
                record: dict[str, Any] = {
                    **plan.record(),
                    "rerun_g4": plan.rerun_g4,
                    "intersection": list(plan.intersection),
                    "changed": list(plan.changed),
                    "patched": list(plan.patched),
                    "tests": list(plan.tests),
                }
            except (GraphError, OSError) as error:  # no graph here: re-run everything
                record = {
                    "G1": "rerun",
                    "G2": "rerun",
                    "G3": "rerun:all",
                    "G4": "rerun:graph-unavailable",
                    "G7": "final-head",
                    "rerun_g4": True,
                    "graph_error": str(error)[:300],
                }
            record.update(
                {
                    "repo": repo,
                    "old_main": card["base_sha"],
                    "new_main": new_main,
                    "g4": previous.get("g4") or {},
                }
            )
            case.put("reverify", out.hypothesis, record, by="auditor")
            self.ledger.append(
                {
                    "type": "reverify",
                    "incident": out.incident_id,
                    "hypothesis": out.hypothesis,
                    "record": {k: v for k, v in record.items() if k.startswith("G")},
                }
            )

    def do_ship(self, step: StepContext, out: c.SurgeonOutcome) -> c.Shipment:
        if self.shipper is None:
            return c.Shipment(out.incident_id, out.hypothesis, out.branch, None, False, False)
        return self.shipper.ship(out)

    def do_canary_sample(self, step: StepContext, shipment: c.Shipment, batch: int) -> list[bool]:
        """One live-traffic batch on the merged code: which tasks failed."""
        import importlib

        host = self.ctx.incidents.host_of(shipment.incident_id) or self._host_of_case(shipment)
        target = self.ctx.target(host)
        target.make_importable()
        pool = importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")
        start = (batch * CANARY_TASKS) % max(1, len(pool))
        tasks = (pool[start:] + pool[:start])[:CANARY_TASKS]
        sha = shipment.merge_sha or shipment.branch
        with clean_checkout(target.repo, sha) as checkout:
            runs = run_tasks(target, checkout, tasks, seed=5000 + batch, phase="canary").runs
        return [not run.passed for run in runs]

    # ---- ledger-shaped steps -----------------------------------------------------------------

    def do_record_transition(self, step: StepContext, record: c.TransitionRecord) -> None:
        self.ledger.append(
            {"type": "transition", "incident": record.incident_id, "transition": _plain(record)}
        )

    def do_park(self, step: StepContext, sig: c.Signature, reason: str) -> None:
        self.ledger.append({"type": "park", "incident": sig.incident_id, "reason": reason})

    def do_escalate(self, step: StepContext, incident: str, reason: str) -> None:
        case = self.ctx.case(incident)
        view = case.reader("coordinator")
        dossier = {name: view.get("decision", name) for name in view.names("decision")}
        self.ledger.append(
            {
                "type": "escalate",
                "incident": incident,
                "reason": reason,
                "decisions": dossier,
                "spent_usd": case.spent(),
            }
        )

    def do_ledger_append(self, step: StepContext, entry: dict[str, Any]) -> str:
        return self.ledger.append(entry)

    # ---- Stage 7 (antibodies, canary actions, vaccination) --------------------------------------

    def do_revert(self, step: StepContext, shipment: c.Shipment) -> bool:
        return False  # the revert path (L3) is Stage 7

    def do_quarantine_related(self, step: StepContext, shipment: c.Shipment) -> int:
        return 0

    def do_synthesize_antibody(
        self, step: StepContext, out: c.SurgeonOutcome, cause: c.Cause
    ) -> c.AntibodyRef:
        ref = c.AntibodyRef(f"AB-{out.incident_id}", "0.0")
        self.ledger.append(
            {
                "type": "antibody",
                "incident": out.incident_id,
                "antibody": f"{ref.antibody_id}@{ref.version}",
                "state": "pending-stage-7",
            }
        )
        return ref

    def do_motif_scan(self, step: StepContext, ab: c.AntibodyRef) -> list[str]:
        return []

    def do_apply_antibody(self, step: StepContext, ab: c.AntibodyRef, host: str) -> Any:
        return c.VaccinationResult(ab.antibody_id, host, False, "vaccination is Stage 7")

    def do_backfill_receptor(self, step: StepContext, ab: c.AntibodyRef) -> None:
        return None

    def do_fixes_resolve(self, step: StepContext, sig: c.Signature, shipment: c.Shipment) -> None:
        return None

    # ---- helpers ------------------------------------------------------------------------------

    def _host(self, out: c.SurgeonOutcome) -> str:
        card = self.ctx.case(out.incident_id).reader("auditor").get("card", out.hypothesis)
        return str(card["host"])

    def _host_of_case(self, shipment: c.Shipment) -> str:
        card = (
            self.ctx.case(shipment.incident_id).reader("auditor").get("card", shipment.hypothesis)
        )
        return str(card["host"])


def _plain(value: Any) -> dict[str, Any]:
    from kintsugi.crew.casefile import plain

    return dict(plain(value))
