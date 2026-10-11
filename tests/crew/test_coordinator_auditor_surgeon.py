"""The Coordinator (plan, cards, decision rule, pre-registered disputes), the blind Auditor and
the Surgeon session runner, on the Ledger practice repository."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kintsugi.control import contracts as c
from kintsugi.control.activities import StepContext
from kintsugi.control.surgeon_step import AttemptLog, SurgeonStep
from kintsugi.crew import claude_session as cs
from kintsugi.crew.auditor import Auditor
from kintsugi.crew.casefile import InformationFlowViolation
from kintsugi.crew.context import CrewContext, LocalTraces
from kintsugi.crew.coordinator import Coordinator, pareto_front
from kintsugi.crew.llm import ScriptedModel
from kintsugi.crew.pathologist import Pathologist
from kintsugi.crew.surgeon import ClaudeSurgeon
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import load_target
from kintsugi.proof import attest
from kintsugi.senses.signature import from_chaos_log, tool_names
from tests.helpers import practice as pr

INC = "INC-0100"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("world")
    p = pr.make_repo(root / "repo")
    buggy = pr.plant_refactor(p.repo)
    pr.git(p.repo, "checkout", "-q", "-b", "fix-hoist")
    fixed = pr.hoist_fix(p.repo)
    pr.git(p.repo, "checkout", "-q", "-b", "fix-lookup", buggy)
    lookup = pr.lookup_fix(p.repo)
    pr.git(p.repo, "checkout", "-q", "-b", "fix-tamper", buggy)
    test = p.repo / "tests" / "test_reconcile.py"
    test.write_text(test.read_text(encoding="utf-8") + "\n# weakened\n", encoding="utf-8")
    tamper = pr.commit_all(p.repo, "tamper")
    pr.git(p.repo, "checkout", "-q", "main")
    identity = root / "holdout.agekey"
    identity.write_text(p.identity + "\n", encoding="ascii")
    target = load_target(p.repo, pr.HOST)
    with clean_checkout(p.repo, buggy) as checkout:
        import importlib

        train = importlib.import_module("practice_ledger.evals.generate").tasks("train")
        live = run_tasks(target, checkout, train, seed=1, phase="live")
    obs = from_chaos_log(
        live.chaos_log,
        live.verdicts(),
        host=pr.HOST,
        split="train",
        seed=1,
        run="live1",
        phase="live",
        services=tool_names(target.host.tools),
        detection_ids=["det-r7"],
    )
    LocalTraces.write(root / "obs", "live1", obs)
    ctx = CrewContext(
        state=root / "crew",
        targets={pr.HOST: target},
        traces=LocalTraces(root / "obs"),
        holdout_identity=identity,
    )
    hits = tuple(
        c.Hit(f"k{i}", "det-r7", o.trace_id, "s", "poller", "R-007", pr.HOST)
        for i, o in enumerate(obs)
    )
    sig = Pathologist(ctx).triage(c.Alert(INC, pr.HOST, hits))
    assert Pathologist(ctx).reproduce(sig).ok
    return {
        "ctx": ctx,
        "sig": sig,
        "repo": p.repo,
        "buggy": buggy,
        "fixed": fixed,
        "lookup": lookup,
        "tamper": tamper,
        "root": root,
    }


CARDS = {
    "cards": [
        {
            "title": "hoist the session",
            "root_cause": "session built per retry",
            "approach": "construct it once",
            "intent_constraints": [],
            "diff_budget": 20,
            "allowed_files": ["practice_ledger/reconcile.py", "tests/test_reconcile.py"],
        },
        {
            "title": "idempotent booking",
            "root_cause": "no idempotency",
            "approach": "check books",
            "intent_constraints": [],
            "diff_budget": 300,
            "allowed_files": ["practice_ledger/reconcile.py", "practice_ledger/tools.py"],
        },
        {
            "title": "big rewrite",
            "root_cause": "x",
            "approach": "y",
            "intent_constraints": [],
            "diff_budget": 300,
            "large_diff_reason": "the module needs restructuring",
            "allowed_files": [".github/workflows/ci.yml"],
        },
    ]
}


def test_the_plan_and_the_cards(world: dict[str, Any]) -> None:
    ctx, sig = world["ctx"], world["sig"]
    coordinator = Coordinator(ctx)
    plan = coordinator.plan_budget(sig, c.Cause(("reconcile",), "sha256:x"))
    assert plan.k == 3
    assert 0 < plan.budget_usd <= 299 * 0.3
    assert plan.plan_sha.startswith("sha256:")
    stored = ctx.case(INC).reader("governor").get("plan", "plan")
    assert stored["plan_sha"] == plan.plan_sha
    model = ScriptedModel({"coordinator": CARDS})
    ctx.model = model
    try:
        cards = coordinator.hypothesize(sig, c.Cause(("reconcile",)), plan, 0, [], None)
        later = coordinator.hypothesize(sig, c.Cause(("reconcile",)), plan, 1, [], None)
    finally:
        ctx.model = None
    assert [card.hypothesis for card in cards + later] == ["H1", "H2", "H3", "H4", "H5", "H6"]
    briefs = [json.loads(card.brief) for card in cards]
    assert briefs[0]["allowed_files"] == ["practice_ledger/reconcile.py"]  # tests/ removed
    assert (briefs[1]["diff_budget"], briefs[1]["approved_large_diff"]) == (120, False)
    assert (briefs[2]["diff_budget"], briefs[2]["approved_large_diff"]) == (300, True)
    assert ".github/workflows/ci.yml" not in briefs[2]["allowed_files"]
    assert cards[0].base_sha == world["buggy"]
    assert "HO-0" not in model.calls[0].prompt  # no holdout task reaches the Coordinator


def _verdict(ctx: CrewContext, hyp: str, g5: str, g4: dict[str, float], commit: str) -> None:
    gates = {g: {"status": "pass"} for g in ("G0", "G1", "G2", "G3", "G4", "G6")}
    gates["G5"] = {"status": g5}
    ctx.case(INC).put(
        "verdict",
        hyp,
        {"hypothesis": hyp, "gates": gates, "g4": g4, "commit": commit, "diff_lines": 4},
        by="auditor",
    )


def test_the_decision_rule_and_a_pre_registered_dispute(world: dict[str, Any]) -> None:
    ctx, sig = world["ctx"], world["sig"]
    _verdict(
        ctx,
        "H11",
        "warn",
        {"delta_pass": 0.3, "cost_delta": -0.1, "p95_delta": -0.1},
        world["buggy"],
    )
    _verdict(
        ctx, "H12", "pass", {"delta_pass": 0.3, "cost_delta": 0.0, "p95_delta": 0.0}, world["fixed"]
    )
    _verdict(
        ctx,
        "H13",
        "pass",
        {"delta_pass": 0.2, "cost_delta": 0.1, "p95_delta": 0.1},
        world["lookup"],
    )
    _verdict(
        ctx,
        "H14",
        "pass",
        {"delta_pass": 0.31, "cost_delta": 0.3, "p95_delta": 0.2},
        world["lookup"],
    )
    v = [c.GateVerdict(h, True) for h in ("H11", "H12", "H13")]
    coordinator = Coordinator(ctx)
    decision = coordinator.coordinate(sig, 0, v)
    assert decision.winner == "H12"  # H11 is a G5 fallback, H13 is dominated
    assert not coordinator.coordinate(sig, 0, [c.GateVerdict("H11", True)]).winner
    assert coordinator.coordinate(sig, 2, [c.GateVerdict("H11", True)]).winner == "H11"
    dispute = coordinator.coordinate(
        sig, 1, [c.GateVerdict("H12", True), c.GateVerdict("H14", True)]
    )
    assert dispute.dispute
    assert dispute.winner is None
    assert dispute.experiment_sha is not None
    settled = coordinator.dispute_experiment(INC, dispute)
    assert settled.winner == "H12"  # both fix the duplicates; the hoist is cheaper
    result = (
        ctx.case(INC)
        .reader("coordinator")
        .get("experiment", dispute.experiment_sha.removeprefix("sha256:")[:16] + "-result")
    )
    assert {r["hypothesis"] for r in result["results"]} == {"H12", "H14"}
    name = dispute.experiment_sha.removeprefix("sha256:")[:16]
    stored = ctx.case(INC).reader("coordinator").get("experiment", name)
    ctx.case(INC).put("experiment", name, {**stored, "runs_per_candidate": 3}, by="coordinator")
    with pytest.raises(ValueError, match="changed after registration"):
        coordinator.dispute_experiment(INC, dispute)
    assert len(pareto_front([{"g4": {"delta_pass": 1}}, {"g4": {"delta_pass": 1}}])) == 2


def _card(world: dict[str, Any], hyp: str, files: list[str]) -> c.Card:
    brief = {
        "hypothesis": hyp,
        "title": "t",
        "root_cause": "r",
        "approach": "a",
        "intent_constraints": [],
        "allowed_files": files,
        "diff_budget": 40,
        "approved_large_diff": False,
        "host": pr.HOST,
        "base_sha": world["buggy"],
        "round": 0,
    }
    world["ctx"].case(INC).put("card", hyp, brief, by="coordinator")
    return c.Card(INC, hyp, 0, json.dumps(brief), world["buggy"])


def test_the_auditor_is_blind_and_strict(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    Coordinator(ctx).plan_budget(world["sig"], c.Cause(("reconcile",)))
    reconcile = ["practice_ledger/reconcile.py"]
    _card(world, "H21", reconcile)
    _card(world, "H22", [*reconcile, "practice_ledger/tools.py"])
    _card(world, "H23", reconcile)
    case = ctx.case(INC)
    case.put("surgeon_report", "H21", {"report": "It is definitely fixed, trust me."}, by="surgeon")
    auditor = Auditor(ctx)
    good = auditor.audit(c.SurgeonOutcome(INC, "H21", 1, "b", "n", world["fixed"]))
    assert good.passed, good.reason
    assert all(good.gates.values())
    costly = auditor.audit(c.SurgeonOutcome(INC, "H22", 1, "b", "n", world["lookup"]))
    assert not costly.passed
    assert not costly.gates["G3"]  # its new books lookup breaks visible tests: G3 flips
    assert not costly.gates["G4"]  # and G4 is skipped once a cheaper gate failed
    assert case.reader("surgeon").get("feedback", "H22")["G3_flips"]
    tamper = auditor.audit(c.SurgeonOutcome(INC, "H23", 1, "b", "n", world["tamper"]))
    assert not tamper.passed
    assert "protected paths" in tamper.reason
    nothing = auditor.audit(c.SurgeonOutcome(INC, "H23", 2, "b", "n", None))
    assert "no attested commit" in nothing.reason
    reads = [a for a in case.accesses() if a["role"] == "auditor"]
    assert reads
    assert not {"surgeon_report", "surgeon_session"} & {a["kind"] for a in reads}
    feedback = case.reader("surgeon").get("feedback", "H21")
    assert set(feedback["G4"]) == {"delta_pass", "ci_before", "ci_after", "cost_delta", "p95_delta"}
    assert "HO-0" not in json.dumps(feedback)
    with pytest.raises(InformationFlowViolation):
        case.reader("auditor").get("surgeon_report", "H21")


def _fake_session(edit: str, text: str) -> Callable[..., cs.SessionResult]:
    def run(request: cs.SessionRequest, on_start: Any = None) -> cs.SessionResult:
        if on_start is not None:
            on_start(12345)
        path = Path(request.cwd) / edit
        path.write_text(text, encoding="utf-8", newline="\n")  # as Claude's Edit tool does
        stream = cs.StreamSummary(session_id=request.session_id)
        stream.tool_uses = [{"name": "Edit", "input": {"file_path": str(path)}}]
        stream.result = {"result": "done", "total_cost_usd": 0.75}
        return cs.SessionResult(request, stream, 0, False, False, None, None)

    return run


def test_the_surgeon_runner_commits_an_attested_change(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    signer = attest.Ed25519Signer.generate()
    hoisted = (world["repo"] / pr.RECONCILE).read_text(encoding="utf-8")
    hoisted = hoisted.replace(pr.NESTED, pr.HOISTED)
    runner = ClaudeSurgeon(
        ctx, signer, run=_fake_session(pr.RECONCILE, hoisted), require_checkpoint=False
    )
    step = SurgeonStep(world["repo"], AttemptLog(world["root"] / "attempts.sqlite"), runner)
    card = _card(world, "H31", ["practice_ledger/reconcile.py"])
    out = step.run(card, StepContext("surgeon", "k", 1, lambda *_: None))
    assert out.commit_sha, ctx.case(INC).reader("auditor").get("refusal", "H31", None)
    message = pr.git(world["repo"], "log", "-1", "--format=%B", out.commit_sha)
    trailers = attest.parse_trailers(message)
    assert trailers["Kintsugi-Role"] == ["surgeon/H31"]
    assert trailers["Kintsugi-Nonce"] == [out.nonce]
    assert "co-authored-by" not in message.lower()
    case = ctx.case(INC)
    assert case.reader("auditor").get("outcome", "H31")["commit"] == out.commit_sha
    assert case.costs_by_role()["surgeon"] == pytest.approx(0.75)
    protected = ClaudeSurgeon(
        ctx, signer, run=_fake_session("tests/test_reconcile.py", "x\n"), require_checkpoint=False
    )
    step2 = SurgeonStep(world["repo"], AttemptLog(world["root"] / "attempts.sqlite"), protected)
    blocked = step2.run(
        _card(world, "H32", ["practice_ledger/reconcile.py"]),
        StepContext("surgeon", "k2", 1, lambda *_: None),
    )
    assert blocked.commit_sha is None
    assert "protected paths" in case.reader("auditor").get("refusal", "H32")["reason"]
