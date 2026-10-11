"""Ship (spec §6.5 step 10): issue → PR → G7 → receipt → merge commit; and G7 itself."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kintsugi.control import contracts as c
from kintsugi.control.ship import (
    REPRO_TRAILER,
    GitHubShipper,
    LocalShipper,
    ShipConfig,
    ShipError,
    parse_repro_tasks,
    render_receipt,
)
from kintsugi.crew.context import CrewContext
from kintsugi.gates.target import load_target
from tests.helpers import practice as pr

INC = "INC-0007"
G4 = {
    "pass_before": 0.72,
    "pass_after": 1.0,
    "ci_before": [0.63, 0.8],
    "ci_after": [0.97, 1.0],
    "p": 1.2e-7,
    "cost_before": 0.0031,
    "cost_after": 0.003,
    "cost_delta": -0.02,
    "cost_ub": 0.01,
    "p95_before": 0.41,
    "p95_after": 0.38,
    "p95_delta": -0.07,
    "p95_ub": 0.03,
    "delta_pass": 0.28,
}


class FakeGitHub:
    def __init__(self, checks: list[list[dict[str, Any]]]) -> None:
        self.checks = checks
        self.calls: list[tuple[str, Any]] = []
        self.bodies: dict[int, str] = {}

    def issue_find(self, marker: str) -> int | None:
        self.calls.append(("issue_find", marker))
        return None

    def issue_create(self, title: str, body: str) -> int:
        self.calls.append(("issue_create", title))
        return 7

    def pr_find(self, head: str) -> dict[str, Any] | None:
        return None

    def pr_create(self, base: str, head: str, title: str, body: str) -> dict[str, Any]:
        self.calls.append(("pr_create", (base, head, title)))
        self.bodies[8] = body
        return {"number": 8, "url": "https://github.com/o/r/pull/8"}

    def pr_edit_body(self, number: int, body: str) -> None:
        self.bodies[number] = body

    def pr_checks(self, number: int) -> list[dict[str, Any]]:
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]

    def pr_merge(self, number: int, method: str) -> str | None:
        self.calls.append(("pr_merge", method))
        return "f" * 40


@pytest.fixture(scope="module")
def shipped(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("ship")
    p = pr.make_repo(root / "repo")
    buggy = pr.plant_refactor(p.repo)
    pr.git(p.repo, "checkout", "-q", "-b", f"kintsugi/{INC}/H2")
    fixed = pr.hoist_fix(p.repo)
    pr.git(p.repo, "checkout", "-q", "main")
    remote = root / "remote.git"
    pr.git(root, "init", "-q", "--bare", str(remote))
    pr.git(p.repo, "remote", "add", "origin", str(remote))
    pr.git(p.repo, "push", "-q", "origin", "main:refs/heads/rehearsal/r1/main")
    pr.git(p.repo, "update-ref", f"refs/kintsugi/attempts/{INC}/H1/1", buggy)
    (root / "holdout.agekey").write_text(p.identity + "\n", encoding="ascii")
    ctx = CrewContext(
        state=root / "crew",
        targets={pr.HOST: load_target(p.repo, pr.HOST)},
        holdout_identity=root / "holdout.agekey",
        g4_seeds=(1,),
    )
    case = ctx.case(INC)
    card = {
        "host": pr.HOST,
        "title": "hoist the session",
        "approach": "construct it once",
        "base_sha": buggy,
        "hypothesis": "H2",
        "allowed_files": [pr.RECONCILE],
    }
    case.put("card", "H2", card, by="coordinator")
    case.put("verdict", "H2", {"g4": G4, "commit": fixed, "passed": True}, by="auditor")
    case.put(
        "symptom",
        "summary",
        {
            "summary": "Lines booked twice after a 429.",
            "label": "DuplicateEntry after fx_rate#retry",
        },
        by="pathologist",
    )
    case.put(
        "signature",
        "phi",
        {
            "phi": "sha256:x",
            "trace_ids": ["local:r:TR-003"],
            "fields": {"error_class": "DuplicateEntry"},
        },
        by="pathologist",
    )
    case.put(
        "repro",
        "g0",
        {
            "ok": True,
            "failing": ["TR-003", "TR-011"],
            "tasks": ["TR-003"],
            "seed": 1,
            "split": "train",
        },
        by="pathologist",
    )
    case.put(
        "intent_record",
        "record",
        {
            "stated_goal": "readability",
            "behavioral_contract": [
                {"clause": "same behavior", "quote": "Keep behavior identical"}
            ],
            "checkpoint": "01M4ARCH1V1STTESTCHECKP01T",
        },
        by="archivist",
    )
    case.put("cause", "report", {"suspects": ["reconcile"]}, by="archivist")
    case.put("plan", "plan", {"budget_usd": 12.4, "plan_sha": "sha256:plan"}, by="governor")
    case.put("outcome", "H1", {"commit": buggy, "checkpoint": None}, by="surgeon")
    case.put("outcome", "H2", {"commit": fixed, "checkpoint": None}, by="surgeon")
    return {"ctx": ctx, "repo": p.repo, "remote": remote, "fixed": fixed, "buggy": buggy}


def _out(world: dict[str, Any]) -> c.SurgeonOutcome:
    return c.SurgeonOutcome(INC, "H2", 1, f"kintsugi/{INC}/H2", "nonce", world["fixed"])


def test_ship_waits_for_g7_then_writes_the_receipt_and_merges(shipped: dict[str, Any]) -> None:
    pending = [{"name": "g7 / G7 · ledger-practice", "bucket": "pending", "link": ""}]
    green = [
        {
            "name": "g7 / G7 · ledger-practice",
            "bucket": "pass",
            "link": "https://github.com/o/r/actions/runs/1",
        },
        # the reusable workflow's other mode: skipped, and not a verdict either way
        {"name": "g7 / G7 fleet · ledger-practice", "bucket": "skipping", "link": ""},
    ]
    github = FakeGitHub([pending, pending, green])
    shipper = GitHubShipper(
        shipped["ctx"],
        ShipConfig("o/r", branch_prefix="rehearsal/r1/"),
        github,
        ledger_head=lambda: (5, "ab" * 32),
        sleep=lambda s: None,
    )
    shipment = shipper.ship(_out(shipped))
    assert shipment.g7_ok
    assert shipment.merged
    assert shipment.branch == f"rehearsal/r1/kintsugi/{INC}/H2"
    assert ("pr_merge", "merge") in github.calls  # a merge commit keeps the attested commit
    base, head, title = next(v for k, v in github.calls if k == "pr_create")
    assert (base, head) == ("rehearsal/r1/main", shipment.branch)
    assert title.startswith(f"fix({INC}):")
    body = github.bodies[8]
    assert body.startswith(f"## Kintsugi repair receipt — {INC}")
    assert "Fixes #7" in body
    assert "https://github.com/o/r/actions/runs/1" in body
    assert "McNemar exact p = 1.2e-07" in body
    assert parse_repro_tasks(body) == ["TR-003", "TR-011"]
    assert "Kintsugi-Symptom: DuplicateEntry" in body  # what G7's fresh-seed G2 looks for
    remote = shipped["remote"]
    assert pr.git(remote, "rev-parse", f"refs/heads/{shipment.branch}") == shipped["fixed"]
    assert pr.git(remote, "rev-parse", f"refs/kintsugi/attempts/{INC}/H1/1") == shipped["buggy"]


def test_a_red_g7_blocks_the_merge(shipped: dict[str, Any]) -> None:
    red = [{"name": "g7 / G7 · ledger-practice", "bucket": "fail", "link": "u"}]
    github = FakeGitHub([red])
    shipper = GitHubShipper(
        shipped["ctx"],
        ShipConfig("o/r", branch_prefix="rehearsal/r1/"),
        github,
        ledger_head=lambda: (5, "ab" * 32),
        sleep=lambda s: None,
    )
    shipment = shipper.ship(_out(shipped))
    assert not shipment.g7_ok
    assert not shipment.merged
    assert not any(k == "pr_merge" for k, _ in github.calls)


def test_the_receipt_validates_against_e4() -> None:
    with pytest.raises(ShipError, match=r"E.4"):
        render_receipt({"INC": "bad"}, [])
    assert parse_repro_tasks(f"x\n{REPRO_TRAILER}: A,B\n") == ["A", "B"]
    assert parse_repro_tasks("nothing") == []


def test_the_local_shipper_runs_g7_and_merges(shipped: dict[str, Any]) -> None:
    ctx = shipped["ctx"]
    shipper = LocalShipper(
        ctx, seed=4242, identity=ctx.holdout_identity, ledger_head=lambda: (1, "cd" * 32)
    )
    shipment = shipper.ship(_out(shipped))
    decision = ctx.case(INC).reader("coordinator").get("decision", "ship-H2")
    assert decision["g7"]["passed"], decision["g7"]
    assert {g: v["status"] for g, v in decision["g7"]["gates"].items()} == {
        "G1": "pass",
        "G2": "pass",
        "G3": "pass",
        "G4": "pass",
    }
    g2 = decision["g7"]["gates"]["G2"]["evidence"]
    assert g2["symptom"] == "DuplicateEntry"
    assert g2["base_failures"].get("DuplicateEntry", 0) > 0  # the trigger still bites the base
    assert "DuplicateEntry" not in g2["head_failures"]
    assert shipment.merged
    repo = Path(shipped["repo"])
    assert pr.git(repo, "rev-parse", "HEAD^2") == shipped["fixed"]
    assert "HO-0" not in str(decision)  # aggregates only
