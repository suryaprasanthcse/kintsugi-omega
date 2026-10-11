"""The Surgeon's attempt-N algorithm (spec §6.6): adopt by nonce, or clean up with a new nonce."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from kintsugi.control import contracts as c
from kintsugi.control.activities import StepContext
from kintsugi.control.surgeon_step import AttemptLog, SurgeonStep, find_commit_with_nonce
from kintsugi.proof.passphrase import is_nonce, new_nonce
from kintsugi.sandbox import proc
from kintsugi.sandbox import worktree as wt

IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
CARD = c.Card("INC-0049", "H1", 0, "drill", "main")


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return proc.run(["git", "-C", str(repo), *IDENTITY, *args], timeout_s=60, check=True,
                    input_text=stdin).stdout.strip()  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "host"
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    (path / "reconcile.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def ctx(attempt: int) -> StepContext:
    return StepContext("surgeon", "surgeon:key", attempt, lambda *_: None)


def commit(worktree: Path, nonce: str, name: str = "fix.txt") -> str:
    (worktree / name).write_text("fixed\n", encoding="utf-8")
    git(worktree, "add", name)
    git(worktree, "commit", "-q", "-F", "-", stdin=f"fix\n\nKintsugi-Nonce: {nonce}\n")
    return git(worktree, "rev-parse", "HEAD")


class Runner:
    """A session stand-in: ``behaviour(worktree, nonce, attempt)`` returns the commit or None."""

    def __init__(self, behaviour: Callable[[Path, str, int], str | None]) -> None:
        self.behaviour = behaviour
        self.runs: list[tuple[int, str]] = []

    def run(
        self,
        wt: wt.Worktree,
        card: c.Card,
        nonce: str,
        attempt: int,
        on_start: Callable[[int], None],
    ) -> str | None:
        worktree = wt
        self.runs.append((attempt, nonce))
        on_start(1)  # pid 1 is never killed (it is never this process's own orphan)
        return self.behaviour(worktree.path, nonce, attempt)


def test_a_first_attempt_issues_a_nonce_before_its_session(repo: Path, tmp_path: Path) -> None:
    log = AttemptLog(tmp_path / "attempts.sqlite")
    seen: list[list[str]] = []

    def behaviour(path: Path, nonce: str, attempt: int) -> str:
        seen.append(log.nonces("INC-0049", "H1"))  # recorded before the session ran
        return commit(path, nonce)

    out = SurgeonStep(repo, log, Runner(behaviour)).run(CARD, ctx(1))
    assert seen == [[out.nonce]]
    assert is_nonce(out.nonce)
    assert out.commit_sha is not None
    assert not out.adopted
    assert log.history("INC-0049", "H1") == [(1, "committed", out.commit_sha, "")]


def test_attempt_two_adopts_a_commit_carrying_an_issued_nonce(repo: Path, tmp_path: Path) -> None:
    log = AttemptLog(tmp_path / "attempts.sqlite")
    first = new_nonce()
    log.issue("INC-0049", "H1", 1, first)
    tree = wt.create(repo, "INC-0049", "H1", base="main")
    sha = commit(tree.path, first)  # attempt 1 committed, then its worker was killed
    runner = Runner(lambda *_: pytest.fail("a second session must not start"))
    out = SurgeonStep(repo, log, runner).run(CARD, ctx(2))
    assert out.adopted
    assert (out.commit_sha, out.nonce, out.attempt) == (sha, first, 2)
    assert runner.runs == []
    assert log.history("INC-0049", "H1")[0][1] == "adopted"


def test_attempt_two_kills_the_orphan_resets_and_uses_a_new_nonce(
    repo: Path, tmp_path: Path
) -> None:
    log = AttemptLog(tmp_path / "attempts.sqlite")
    first = new_nonce()
    log.issue("INC-0049", "H1", 1, first)
    tree = wt.create(repo, "INC-0049", "H1", base="main")
    (tree.path / "scratch.txt").write_text("half-done work\n", encoding="utf-8")
    (tree.path / "reconcile.py").write_text("broken\n", encoding="utf-8")
    orphan = proc.start([sys.executable, "-c", "import time; time.sleep(120)"])
    wt.write_lock(tree, orphan.pid)

    def behaviour(path: Path, nonce: str, attempt: int) -> str:
        assert not (path / "scratch.txt").exists()  # cleaned before the new session
        assert (path / "reconcile.py").read_text("utf-8") == "def run():\n    return 1\n"
        return commit(path, nonce)

    out = SurgeonStep(repo, log, Runner(behaviour)).run(CARD, ctx(2))
    assert orphan.wait(timeout_s=10) is not None  # the orphan holding the lock was killed
    assert not out.adopted
    assert out.nonce != first
    assert out.commit_sha is not None
    assert len(out.abandoned) == 1
    assert out.abandoned[0].startswith("attempt 1: no commit, no checkpoint")
    states = [state for _, state, _, _ in log.history("INC-0049", "H1")]
    assert states == ["abandoned", "committed"]


def test_a_commit_with_a_nonce_never_issued_is_not_adopted(repo: Path, tmp_path: Path) -> None:
    log = AttemptLog(tmp_path / "attempts.sqlite")
    log.issue("INC-0049", "H1", 1, new_nonce())
    tree = wt.create(repo, "INC-0049", "H1", base="main")
    commit(tree.path, new_nonce())  # e.g. a human commit: no attestation for that nonce
    issued = log.nonces("INC-0049", "H1")
    assert find_commit_with_nonce(repo, tree.branch, tree.base_sha, issued) is None
    step = SurgeonStep(repo, log, Runner(lambda path, nonce, attempt: commit(path, nonce, "b.txt")))
    result = step.run(CARD, ctx(2))
    assert not result.adopted
