"""The L3 auto-revert on the practice repository (spec §6.7 step 16; plan §2a L3).

A bad change is merged (the practice regression stands in for a wrong vaccination), the canary's
failing runs are known, and the Reverter undoes the merge: git decides the exact tree, a notary
session applies it (attested commit), G1 and the regression G2 pass, and the revert merges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kintsugi.control import contracts as c
from kintsugi.control.revert import RevertConfig, Reverter, revert_tree
from kintsugi.crew.notary import Notary, NotaryError, NotaryRequest
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.proof import attest
from tests.helpers import practice as pr
from tests.helpers.notary import FakeNotarySession


@pytest.fixture(scope="module")
def merged(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("revert")
    p = pr.make_repo(root / "repo")
    pr.git(p.repo, "checkout", "-q", "-b", "kintsugi/VAC-1/apply")
    pr.plant_refactor(p.repo)  # the "wrong fix"
    pr.git(p.repo, "checkout", "-q", "main")
    pr.git(p.repo, *pr.IDENTITY, "merge", "--no-ff", "-q", "-m", "Merge VAC-1",
           "kintsugi/VAC-1/apply")  # fmt: skip
    merge = pr.git(p.repo, "rev-parse", "HEAD")
    target = p.target
    import importlib

    target.make_importable()
    tasks = importlib.import_module("practice_ledger.evals.generate").tasks("train")
    with clean_checkout(p.repo, merge) as checkout:
        failing = run_tasks(target, checkout, tasks, seed=1, phase="canary").failed()
    assert failing, "the merged change must fail some canary runs"
    return {"p": p, "merge": merge, "failing": failing}


def test_the_revert_restores_the_first_parent_through_a_notary(merged: dict[str, Any]) -> None:
    p, merge = merged["p"], merged["merge"]
    signer = attest.Ed25519Signer.generate()
    session = FakeNotarySession()
    notary = Notary(signer, run=session, require_checkpoint=False)
    reverter = Reverter(notary, RevertConfig(require_g7=False))
    shipment = c.Shipment("VAC-1", "AB-1@1.0", "kintsugi/VAC-1/apply", None, True, True, merge)
    record = reverter.revert(
        p.target, shipment, record="REV-VAC-1", title="AB-1@1.0 on ledger-practice",
        why="The canary's SPRT rejected AB-1@1.0 on ledger-practice at n=8.",
        cases=[(1, t) for t in merged["failing"]],
    )  # fmt: skip
    assert record.ok, record
    assert record.gates["G1"]["passed"]
    assert record.gates["G2"]["passed"]
    assert record.gates["G2"]["cases"] == len(merged["failing"])
    first_parent_tree = pr.git(p.repo, "rev-parse", f"{merge}^1^{{tree}}")
    assert record.commit is not None
    assert pr.git(p.repo, "rev-parse", f"{record.commit}^{{tree}}") == first_parent_tree
    message = pr.git(p.repo, "log", "-1", "--format=%B", record.commit)
    assert message.startswith("Revert AB-1@1.0 on ledger-practice")
    assert f"This reverts merge commit {merge}" in message
    assert attest.parse_trailers(message)["Kintsugi-Role"] == ["notary/revert"]
    assert "Session nonce: " in session.prompts[0]
    assert pr.git(p.repo, "rev-parse", "HEAD") == record.merge_sha  # merged locally
    assert record.shipment("VAC-1").merged


def test_a_notary_that_changes_anything_else_is_refused(
    merged: dict[str, Any], tmp_path: Path
) -> None:
    p = pr.make_repo(tmp_path / "repo")
    pr.git(p.repo, "checkout", "-q", "-b", "bad")
    pr.plant_refactor(p.repo)
    pr.git(p.repo, "checkout", "-q", "main")
    pr.git(p.repo, *pr.IDENTITY, "merge", "--no-ff", "-q", "-m", "Merge bad", "bad")
    merge = pr.git(p.repo, "rev-parse", "HEAD")
    expected = revert_tree(p.repo, merge, merge)
    notary = Notary(attest.Ed25519Signer.generate(), run=FakeNotarySession(tamper=True),
                    require_checkpoint=False)  # fmt: skip
    pr.git(p.repo, "checkout", "-q", "-b", "rev")
    with pytest.raises(NotaryError, match="not the decided"):
        notary.commit(p.repo, NotaryRequest(
            operation="revert", reference=merge[:12], incident="REV-X", purpose="Revert.",
            diff=pr.git(p.repo, "diff", merge, f"{merge}^1") + "\n", subject="Revert bad",
            expected_tree=expected))  # fmt: skip
    assert pr.git(p.repo, "rev-parse", "HEAD") == merge  # nothing kept
