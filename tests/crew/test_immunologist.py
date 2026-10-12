"""The Immunologist on the practice hosts (spec §5.7, §6.5 steps 12–14, §6.7 steps 17–19).

Graphs come from the golden snapshots (no Entire needed): the origin before its fix
(``ledger_practice_before``), the CRM practice host, Scout. Everything else is real: case files,
the library, task runs under the induced trigger, G0 on its cassette.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from kintsugi.crew.casefile import InformationFlowViolation
from kintsugi.crew.context import CrewContext
from kintsugi.crew.immunologist import (
    History,
    Immunologist,
    Record,
    receptor_body,
    receptor_rule,
    render_recipe,
    score_receptor,
)
from kintsugi.gates.checkout import rev
from kintsugi.gates.target import load_target
from kintsugi.memory.antibody import AntibodyState
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.senses import contract
from tests.helpers import practice as pr
from tests.helpers.graphs import REHEARSAL, host_graph, source_tree

OMEGA = Path(__file__).resolve().parents[2]
INC = "INC-R1-0001"


def _crm_repo(destination: Path) -> Path:
    shutil.copytree(
        REHEARSAL / "crm_practice", destination, ignore=shutil.ignore_patterns("__pycache__")
    )
    pr.git(destination, "init", "-q", "-b", "main")
    pr.git(destination, "config", "core.autocrlf", "false")
    pr.commit_all(destination, "crm practice")
    return destination


def _history() -> History:
    records = [
        Record(f"lp-{i}", "ledger-practice", "FAIL:DUPLICATE_ENTRY", "live", INC) for i in range(4)
    ]
    records += [
        Record(f"crm-{i}", "crm-practice", "FAIL:DUPLICATE_CONTACT", "baseline") for i in range(3)
    ]
    records += [Record(f"sc-{i}", "scout", "PASS", "baseline") for i in range(5)]
    records += [Record("tr-1", "triage", "FAIL:WRONG_LABEL", "baseline")]
    return History(records)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = Path(tmp_path_factory.mktemp("immunologist"))
    p = pr.make_repo(root / "ledger")
    before = pr.plant_refactor(p.repo)
    pr.write(
        p.repo,
        pr.RECONCILE,
        (p.repo / pr.RECONCILE).read_text("utf-8").replace(pr.NESTED, pr.HOISTED),
    )
    after = pr.commit_all(p.repo, "fix: hoist the session")
    crm = _crm_repo(root / "crm")
    targets = {
        "ledger-practice": p.target,
        "crm-practice": load_target(crm, "crm-practice"),
        "scout": load_target(OMEGA, "scout"),
    }
    ctx = CrewContext(state=root / "state", targets=targets)
    lib = AntibodyLibrary(root / "antibodies")
    imm = Immunologist(
        ctx,
        lib,
        history=_history(),
        positives={
            "ledger@pre-fix": host_graph(
                "ledger_before", source_tree("ledger_before", root / "g" / "lb")
            )
        },
    )
    graphs = {
        name: host_graph(name, source_tree(name, root / "g" / name))
        for name in ("ledger_practice_before", "ledger_after", "crm_practice", "scout")
    }
    imm._graphs.update(
        {
            ("ledger-practice", before): graphs["ledger_practice_before"],
            ("ledger-practice", after): graphs["ledger_after"],  # stand-in: the motif is gone
            ("ledger-practice", rev(p.repo, "main")): graphs["ledger_after"],
            ("crm-practice", rev(crm, "main")): graphs["crm_practice"],
            ("scout", rev(OMEGA, "main")): graphs["scout"],
        }
    )
    case = ctx.case(INC)
    fields = {
        "error_class": "DuplicateEntry",
        "failing_span": ["CHAIN", "grader.verdict"],
        "tool": "books:/lines",
        "preceding": ["fx_rate", "books:/lines", "books:/lines"],
        "detections": ["omega:R-007:ledger-practice"],
    }
    case.put(
        "signature",
        "phi",
        {"phi": "sha256:phi-1", "fields": fields, "label": "dup", "trace_ids": ["t1", "t2"]},
        by="pathologist",
    )
    case.put(
        "observations",
        "pass0",
        {
            "clusters": [
                {
                    "fields": fields,
                    "members": [
                        {"verdict": "FAIL:DUPLICATE_ENTRY", "trace_id": "t1"},
                        {"verdict": "FAIL:DUPLICATE_ENTRY", "trace_id": "t2"},
                    ],
                }
            ]
        },
        by="pathologist",
    )
    case.put("symptom", "summary", {"summary": "duplicate lines after a 429"}, by="pathologist")
    case.put(
        "cause",
        "report",
        {
            "suspects": ["book_pending_lines", "reconcile"],
            "mechanism": "the session is rebuilt on every retry",
            "originating_commit": before,
            "first_bad_checkpoint": "01ABC",
        },
        by="archivist",
    )
    case.put(
        "intent_record",
        "record",
        {
            "behavioral_contract": [
                {"clause": "a readability refactor", "quote": "name the retried pass"}
            ],
            "checkpoint": "01ABC",
        },
        by="archivist",
    )
    case.put(
        "card",
        "H2",
        {
            "host": "ledger-practice",
            "base_sha": before,
            "title": "hoist",
            "approach": "hoist the session",
        },
        by="coordinator",
    )
    case.put("outcome", "H2", {"commit": after, "checkpoint": "01DEF"}, by="surgeon")
    case.put("verdict", "H2", {"g4": {"b": 39, "c": 0, "p": 1.8e-12}}, by="auditor")
    case.put(
        "decision",
        "ship-H2",
        {"pr": "https://github.com/o/r/pull/5", "g7": "https://ci/1"},
        by="shipper",
    )
    case.add_cost("surgeon", 1.25, "H2")
    from kintsugi.control import contracts as c

    out = c.SurgeonOutcome(INC, "H2", 1, "kintsugi/INC/H2", "n", after, "01DEF")
    ab = imm.synthesize(out, merge_sha="m" * 40)
    return {"imm": imm, "lib": lib, "ab": ab, "ctx": ctx, "crm": crm, "before": before}


def test_receptor_rules_name_the_verdict_family() -> None:
    assert receptor_rule(["FAIL:DUPLICATE_ENTRY", "FAIL:DUPLICATE_ENTRY"]) == '^"?FAIL:DUPLICATE_'
    assert receptor_rule(["FAIL:DUPLICATE_ENTRY", "FAIL:MISSING_ENTRY"]) == (
        '^"?FAIL:(DUPLICATE_ENTRY|MISSING_ENTRY)'
    )
    contract.validate_request("detections.create", receptor_body("AB-1", '^"?FAIL:X', "d"))


def test_synthesis_builds_a_validated_antibody(world: dict[str, Any]) -> None:
    ab, lib = world["ab"], world["lib"]
    assert ab.ref == "AB-1@1.0"
    assert ab.state is AntibodyState.CANDIDATE
    assert ab.evidence["binding"] == {
        "R": "retry_with_backoff",
        "Enc": "reconcile",
        "F": "book_pending_lines",
        "S": "PostingSession",
    }
    assert ab.receptor.rule == '^"?FAIL:DUPLICATE_'
    assert {"{S}", "{F}", "{R}"} <= {s for s in ("{S}", "{F}", "{R}") if s in ab.recipe}
    assert ab.economics.primary_usd == 1.25
    evidence = lib.evidence("AB-1")
    assert evidence["motif_validation"]["matches_after_fix"] is False
    assert evidence["prs"] == ["https://github.com/o/r/pull/5"]
    assert evidence["checkpoints"] == {"intent": "01ABC", "repair": "01DEF", "originating": "01ABC"}
    # At synthesis only the origin is known vulnerable: the CRM's duplicates are unexplained.
    assert evidence["receptor_precision"]["precision"] == pytest.approx(4 / 7)
    assert lib.index()["signatures"]["sha256:phi-1"] == "AB-1@1.0"
    rendered = render_recipe(ab.recipe, ab.evidence["binding"], intent="keep it")
    assert "PostingSession" in rendered
    assert "{" not in rendered


def test_the_immunologist_never_reads_surgeon_reasoning(world: dict[str, Any]) -> None:
    case = world["ctx"].case(INC)
    case.put("surgeon_report", "H2", {"report": "trust me"}, by="surgeon")
    with pytest.raises(InformationFlowViolation):
        case.reader("immunologist").get("surgeon_report", "H2")


def test_scan_backfill_and_promotion(world: dict[str, Any]) -> None:
    imm, ab = world["imm"], world["ab"]
    assert imm.scan(ab) == ["crm-practice", "scout"]  # v1.0 also binds the look-alike
    backfill = imm.backfill(ab)
    assert backfill["silent"] == 3
    assert backfill["silent_by_host"] == {"crm-practice": 3}
    assert backfill["impact_usd"] == 6.0
    promoted = imm.promote("AB-1", "crm-practice", why="matured on crm-practice")
    assert promoted.state is AntibodyState.ACTIVE  # crm is now known vulnerable: precision 1.0
    assert "crm-practice" in promoted.lineage.hosts_protected
    states = [
        (e.get("from"), e.get("to"))
        for e in world["lib"].lineage("AB-1")["events"]
        if e["event"] == "state"
    ]
    assert states == [("CANDIDATE", "MATURED"), ("MATURED", "ACTIVE")]


def test_the_induced_trigger_and_the_vaccination_case(world: dict[str, Any]) -> None:
    imm, ab = world["imm"], world["lib"].load("AB-1")
    trigger = imm.induced_trigger(ab, "crm-practice")
    assert trigger.services == ("geo",)
    assert trigger.produced_failure
    assert any(v.startswith("FAIL:DUPLICATE_CONTACT") for v in trigger.classes)
    case, card = imm.prepare(ab, "crm-practice", trigger)
    view = case.reader("auditor")
    assert view.get("repro", "g0")["ok"]
    assert view.get("cassette", "g0")
    stored = view.get("card", card.hypothesis)
    assert stored["allowed_files"] == ["practice_crm/sync.py"]
    assert "SyncSession" in stored["approach"]
    assert "{S}" not in stored["approach"]
    assert world["ctx"].incidents.host_of(card.incident_id) == "crm-practice"


def test_quarantine_keeps_the_receptor_and_tells_open_prs(world: dict[str, Any]) -> None:
    class GitHub:
        def __init__(self) -> None:
            self.comments: list[tuple[int, str]] = []

        def pr_list_open(self, prefix: str) -> list[dict[str, Any]]:
            assert prefix == "rehearsal/r1/kintsugi/VAC-AB-1-"
            return [{"number": 9, "url": "https://github.com/o/r/pull/9"}]

        def pr_comment(self, number: int, body: str) -> None:
            self.comments.append((number, body))

    imm, lib = world["imm"], world["lib"]
    lib.protect("AB-1", "scout")
    gh = GitHub()
    result = imm.quarantine(
        "AB-1",
        "scout",
        why="Canary SPRT rejected it on scout at n=8.",
        github=gh,
        pr_prefix="rehearsal/r1/",
    )
    assert result["state"] == "QUARANTINED"
    assert result["receptor_active"]
    assert gh.comments[0][0] == 9
    assert gh.comments[0][1].startswith("On hold: AB-1@1.0 quarantined pending refinement.")
    ab = lib.load("AB-1")
    assert ab.lineage.quarantines == 1
    assert "scout" not in ab.lineage.hosts_protected


def test_refinement_learns_the_side_effect_precondition(world: dict[str, Any]) -> None:
    imm, lib = world["imm"], world["lib"]
    scout = imm.graph("scout")
    revised, learned = imm.refine(
        "AB-1",
        ("scout", scout),
        trigger="canary SPRT reject on scout",
        revert_pr="https://github.com/o/r/pull/10",
        trace_to_test="scout/evals/regressions/x.json",
    )
    assert revised.version == "1.1"
    assert revised.preconditions == ["a side-effecting callee inside the retried scope"]
    assert learned.selected == ("side_effect_in_scope",)
    assert set(learned.table["side_effect_in_scope"]) == {
        "ledger-practice@pre-fix",
        "ledger@pre-fix",
        "crm-practice",
        "scout",
    }
    assert imm.scan(revised) == []  # crm is protected; scout no longer matches
    assert lib.ref("AB-1@1.0").version == "1.0"  # the quarantined version stays resolvable
    [refinement] = lib.lineage("AB-1")["refinements"]
    assert refinement["learned_precondition"] == list(revised.preconditions)
    assert refinement["revert_pr"] == "https://github.com/o/r/pull/10"
    again = imm.promote("AB-1", "crm-practice", why="re-matured: v1.1 binds crm-practice")
    assert again.state is AntibodyState.ACTIVE
    assert again.lineage.versions == ["1.0", "1.1"]


def test_receptor_scores_count_unexplained_flags_as_false_positives() -> None:
    history = _history()
    score = score_receptor(
        '^"?FAIL:DUPLICATE_', "DuplicateEntry", {"ledger-practice", "crm-practice"}, history
    )
    assert (score.precision, score.flagged, score.true_positives) == (1.0, 7, 7)
    narrow = score_receptor('^"?FAIL:DUPLICATE_', "DuplicateEntry", {"ledger-practice"}, history)
    assert narrow.false_positives == ("crm-0", "crm-1", "crm-2")
