"""G6 stops a round when the incident's budget is spent (spec §5.11; Stage 6 negative test)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kintsugi.control import contracts as c
from kintsugi.control.activities import StepContext
from kintsugi.crew.auditor import Auditor
from kintsugi.crew.context import CrewContext
from kintsugi.crew.coordinator import Coordinator
from kintsugi.crew.factory import OmegaCrew
from kintsugi.gates.target import load_target
from tests.helpers import practice as pr


def test_no_surgeon_session_starts_past_the_budget(tmp_path: Path) -> None:
    p = pr.make_repo(tmp_path / "repo")
    ctx = CrewContext(state=tmp_path / "crew", targets={pr.HOST: load_target(p.repo, pr.HOST)})
    case = ctx.case("INC-G6")
    case.put("plan", "plan", {"budget_usd": 1.0, "plan_sha": "sha256:plan"}, by="governor")
    case.put("repro", "g0", {"ok": True}, by="pathologist")
    case.add_cost("surgeon", 1.25, "round 0 sessions")

    def no_session() -> object:
        pytest.fail("a Surgeon session started after the budget was spent")

    crew = OmegaCrew(ctx, runner=no_session)  # type: ignore[arg-type]
    brief = {"host": pr.HOST, "hypothesis": "H4", "title": "t", "approach": "a",
             "root_cause": "r", "allowed_files": [pr.RECONCILE], "diff_budget": 20,
             "base_sha": p.healthy, "round": 1}  # fmt: skip
    case.put("card", "H4", brief, by="coordinator")
    card = c.Card("INC-G6", "H4", 1, json.dumps(brief), p.healthy)
    out = crew.do_surgeon(StepContext("surgeon", "k", 1, lambda *_: None), card)
    assert out.commit_sha is None
    refusal = case.reader("auditor").get("refusal", "H4")
    assert "G6" in refusal["reason"]
    verdict = Auditor(ctx).audit(out)
    assert not verdict.passed
    assert "budget spent" in verdict.reason
    sig = c.Signature("INC-G6", pr.HOST, "sha256:phi", ("t1",))
    plan = c.BudgetPlan(1.0, 3, 3, "claude-sonnet-5-5", "sha256:plan")
    assert Coordinator(ctx).budget_exhausted(sig, plan, 0)  # the workflow then escalates (L5)
