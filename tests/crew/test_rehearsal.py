"""The Stage 6 rehearsal runner's building blocks (the live run itself is the evidence)."""

from __future__ import annotations

from pathlib import Path

from kintsugi.crew import rehearsal as rh
from kintsugi.proof import passphrase
from kintsugi.sandbox import proc
from kintsugi.senses.signature import FailureObservation
from kintsugi.senses.store import SenseStore
from tests.helpers import practice as pr


def test_the_practice_patch_applies_to_the_healthy_code(tmp_path: Path) -> None:
    p = pr.make_repo(tmp_path / "repo")
    patch = tmp_path / "refactor.patch"
    patch.write_text(rh.refactor_diff(), encoding="utf-8", newline="\n")
    proc.run(["git", "-C", str(p.repo), "apply", str(patch)], timeout_s=60, check=True)
    applied = (p.repo / pr.RECONCILE).read_text(encoding="utf-8")
    assert applied == pr.REFACTOR.read_text(encoding="utf-8")


def test_the_delivery_prompt_states_the_intent_and_carries_the_nonce() -> None:
    nonce = passphrase.new_nonce()
    rendered = rh.delivery_prompt(nonce)
    assert f"Session nonce: {nonce}" in rendered.prompt  # audit C4 finds it in the transcript
    assert "retry logic for readability" in rendered.prompt  # the stated intent (§6.2 step 3)
    assert "+    def book_pending_lines() -> PostingSession:" in rendered.prompt
    assert rendered.template_sha.startswith("sha256:")


def test_the_rehearsal_ruleset_requires_a_pr_and_g7_without_bypass() -> None:
    body = rh.ruleset_body(["refs/heads/rehearsal/b/main", "refs/heads/rehearsal/a/main"])
    types = [rule["type"] for rule in body["rules"]]
    assert {"pull_request", "required_status_checks", "non_fast_forward", "deletion"} == set(types)
    assert "required_linear_history" not in types  # merge commits keep attestations (A-51)
    checks = next(r for r in body["rules"] if r["type"] == "required_status_checks")
    assert checks["parameters"]["required_status_checks"] == [{"context": rh.G7_CONTEXT}]
    assert body["bypass_actors"] == []
    assert body["conditions"]["ref_name"]["include"] == [  # explicit: creation stays possible
        "refs/heads/rehearsal/a/main",
        "refs/heads/rehearsal/b/main",
    ]


def test_failed_tasks_become_claimed_r007_hits() -> None:
    store = SenseStore(":memory:")
    obs = [
        FailureObservation(
            f"local:r:TR-00{n}",
            rh.HOST,
            "CHAIN",
            "grader.verdict",
            "post_line",
            "FAIL:DUPLICATE_ENTRY",
            ("fx_rate",),
        )
        for n in range(3)
    ]
    hits = rh.claim_hits(store, obs)
    assert [h.receptor for h in hits] == ["R-007"] * 3
    assert len(store.hits_for("R-007", rh.HOST)) == 3
    assert hits[0].detected_at is not None
    assert hits[0].detected_at < hits[2].detected_at  # type: ignore[operator]
