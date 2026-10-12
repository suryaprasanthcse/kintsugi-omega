"""The ledger on its append-only branch, its signed hourly anchors, and audit C1 over both."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kintsugi.proof import attest, audit
from kintsugi.proof.anchor import verify_anchor
from kintsugi.proof.audit import Status
from kintsugi.proof.ledger import genesis_hash
from kintsugi.proof.ledger_branch import BranchLedger, LedgerBranchError
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import git

T0 = datetime(2026, 10, 12, 13, 20, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "omega"
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    git(path, "config", "user.name", "Surya Prasanth R")
    git(path, "config", "user.email", "suryaprasanthcse@gmail.com")
    (path / "README.md").write_text("omega\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


@pytest.fixture
def signer() -> attest.Ed25519Signer:
    return attest.Ed25519Signer.generate()


def _book(repo: Path, signer: attest.Ed25519Signer) -> BranchLedger:
    return BranchLedger(repo, genesis_hash(T0.isoformat(), signer.public_key_raw()))


def test_appends_are_commits_on_the_branch_and_leave_main_alone(
    repo: Path, signer: attest.Ed25519Signer
) -> None:
    book = _book(repo, signer)
    main = git(repo, "rev-parse", "main")
    h1 = book.append({"type": "genesis", "t0": T0.isoformat()}, at=T0)
    h2 = book.append({"type": "incident", "incident": "INC-1"}, at=T0 + timedelta(minutes=1))
    entries = book.entries()
    assert [e["seq"] for e in entries] == [1, 2]
    assert entries[1]["prev"] == h1
    assert entries[1]["hash"] == h2
    assert git(repo, "rev-list", "--count", "ledger") == "2"
    assert git(repo, "rev-parse", "main") == main
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "-1", "--format=%s", "ledger") == "ledger: seq 2 incident"


def test_anchor_signs_the_head_once_per_hour(repo: Path, signer: attest.Ed25519Signer) -> None:
    book = _book(repo, signer)
    with pytest.raises(LedgerBranchError, match="nothing to anchor"):
        book.anchor(signer, at=T0)
    book.append({"type": "genesis"}, at=T0)
    first = book.anchor(signer, at=T0)
    book.append({"type": "incident", "incident": "INC-1"}, at=T0)
    assert book.anchor(signer, at=T0 + timedelta(minutes=5)) == first  # same hour: no re-sign
    assert first.seq == 1
    assert verify_anchor(first, attest.load_public_key(signer.public_key_openssh()))
    second = book.anchor(signer, at=T0 + timedelta(hours=1))
    assert second.seq == 2
    assert [a.tag for a in book.anchors()] == ["anchor-20261012-13", "anchor-20261012-14"]


def test_c1_reads_the_branch_and_its_tags(repo: Path, signer: attest.Ed25519Signer) -> None:
    book = _book(repo, signer)
    for i in range(3):
        book.append({"type": "incident", "incident": f"INC-{i}"}, at=T0)
    book.anchor(signer, at=T0)
    window = audit.Window(
        t0=T0.isoformat(),
        repo="o/r",
        branch="main",
        base="HEAD",
        attest_pubkey=signer.public_key_openssh(),
        holdout_manifest="x",
    )
    entries = audit.load_ledger(repo, window, None)
    assert entries is not None
    result = audit.c1_ledger(entries, window.genesis(), audit.load_anchors(repo, None),
                             window.public_key())  # fmt: skip
    assert result.status is Status.PASS, result.problems
    # Rewrite the branch: drop entry 2 and re-chain. The published anchor h_3 exposes it.
    git(repo, "update-ref", "-d", "refs/heads/ledger")
    forged = _book(repo, signer)
    for i in (0, 2, 3):
        forged.append({"type": "incident", "incident": f"INC-{i}"}, at=T0)
    rewritten = audit.load_ledger(repo, window, None)
    assert rewritten is not None
    bad = audit.c1_ledger(rewritten, window.genesis(), audit.load_anchors(repo, None),
                          window.public_key())  # fmt: skip
    assert bad.status is Status.FAIL
    assert "anchor h_3" in bad.problems[0]
