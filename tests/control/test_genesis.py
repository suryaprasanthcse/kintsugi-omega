"""`kintsugi t0 genesis`: the window record, the ledger's seq 1, and W's base for the audit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kintsugi.control.genesis import GenesisError, write_genesis
from kintsugi.proof import attest, audit
from kintsugi.proof.ledger_branch import BranchLedger
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    origin = tmp_path / "origin.git"
    proc.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], timeout_s=30, check=True)
    path = tmp_path / "omega"
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    git(path, "config", "user.name", "Surya Prasanth R")
    git(path, "config", "user.email", "suryaprasanthcse@gmail.com")
    (path / ".kintsugi").mkdir()
    (path / ".kintsugi" / "holdout.sha256").write_text("# manifest\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    git(path, "remote", "add", "origin", str(origin))
    git(path, "push", "-q", "origin", "main")
    return path


def test_genesis_writes_the_window_and_seq_1_once(repo: Path) -> None:
    signer = attest.Ed25519Signer.generate()
    with pytest.raises(GenesisError, match="public key"):
        write_genesis(repo, github_repo="o/r", rulesets=[7])
    (repo / ".kintsugi" / "attest.pub").write_text(signer.public_key_openssh() + "\n",
                                                   encoding="utf-8")  # fmt: skip
    t0 = (datetime.now(UTC) - timedelta(minutes=1)).replace(microsecond=0)
    made = write_genesis(repo, github_repo="o/r", rulesets=[7], t0=t0.isoformat())
    record = json.loads((repo / audit.WINDOW_FILE).read_text(encoding="utf-8"))
    assert record["base"] == git(repo, "rev-parse", "origin/main")
    assert record["rulesets"] == [7]
    assert record["holdout_manifest"] == audit.manifest_digest(repo)
    entries = BranchLedger(repo, made.genesis).entries()
    assert [e["type"] for e in entries] == ["genesis"]
    assert entries[0]["prev"] == made.genesis == made.window.genesis()
    with pytest.raises(GenesisError, match="once"):
        write_genesis(repo, github_repo="o/r", rulesets=[7])
    # The T0 commit is W's base: the audit sees only what follows it.
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "T0")
    t0_commit = git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "in W")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    inside = audit.window_commits(repo, made.window, datetime.now(UTC) + timedelta(minutes=1))
    assert inside == [git(repo, "rev-parse", "HEAD")]
    assert t0_commit not in inside


def test_genesis_needs_a_ruleset(repo: Path) -> None:
    signer = attest.Ed25519Signer.generate()
    (repo / ".kintsugi" / "attest.pub").write_text(signer.public_key_openssh(), encoding="utf-8")
    with pytest.raises(GenesisError, match="ruleset"):
        write_genesis(repo, github_repo="o/r", rulesets=[])
