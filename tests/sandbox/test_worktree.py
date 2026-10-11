"""Disposable attempt worktrees: rewind keeps evidence, L2 reset is confined to wt/."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from kintsugi.sandbox import proc
from kintsugi.sandbox import worktree as wt

IDENTITY = ["-c", "user.name=Kintsugi Test", "-c", "user.email=test@example.invalid"]


def _git(repo: Path, *args: str) -> str:
    result = proc.run(["git", "-C", str(repo), *IDENTITY, *args], timeout_s=60)
    assert result.ok, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "host"
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    (path / "reconcile.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


def _commit_in(worktree: wt.Worktree, name: str, text: str) -> str:
    (worktree.path / name).write_text(text, encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-q", "-m", f"attempt: {name}")
    return worktree.head()


def test_create_makes_the_spec_layout(repo: Path) -> None:
    attempt = wt.create(repo, "INC-0042", "H2")
    assert attempt.path == repo / "wt" / "INC-0042" / "H2"
    assert attempt.branch == "kintsugi/INC-0042/H2"
    assert attempt.base_sha == _git(repo, "rev-parse", "HEAD")
    assert not attempt.has_new_commits()
    assert _git(repo, "status", "--porcelain") == ""  # wt/ is excluded from the main worktree


def test_rewind_pins_the_attempt_and_removes_the_worktree(repo: Path) -> None:
    attempt = wt.create(repo, "INC-0042", "H2")
    head = _commit_in(attempt, "fix.py", "x = 1\n")
    record = wt.discard(attempt)
    assert record.pinned_ref == "refs/kintsugi/attempts/INC-0042/H2/1"
    assert record.head == head
    assert record.removed
    assert _git(repo, "rev-parse", record.pinned_ref) == head  # the evidence stays reachable
    assert "kintsugi/INC-0042/H2" not in _git(repo, "branch", "--list")


def test_attempts_are_numbered_and_never_overwritten(repo: Path) -> None:
    first = wt.create(repo, "INC-1", "H1")
    _commit_in(first, "a.py", "a = 1\n")
    wt.discard(first)
    second = wt.create(repo, "INC-1", "H1")
    head = _commit_in(second, "b.py", "b = 2\n")
    assert wt.discard(second).pinned_ref == "refs/kintsugi/attempts/INC-1/H1/2"
    assert wt.attempt_count(repo, "INC-1", "H1") == 2
    assert _git(repo, "rev-parse", "refs/kintsugi/attempts/INC-1/H1/2") == head


def test_an_attempt_without_commits_leaves_no_ref(repo: Path) -> None:
    attempt = wt.create(repo, "INC-2", "H1")
    record = wt.discard(attempt)
    assert record.pinned_ref is None
    assert wt.attempt_count(repo, "INC-2", "H1") == 0


def test_l2_reset_clean_restores_the_base(repo: Path) -> None:
    attempt = wt.create(repo, "INC-3", "H1")
    (attempt.path / "reconcile.py").write_text("broken\n", encoding="utf-8")
    (attempt.path / "scratch.tmp").write_text("partial\n", encoding="utf-8")
    wt.reset_clean(attempt)
    restored = (attempt.path / "reconcile.py").read_text(encoding="utf-8")
    assert restored == "def run():\n    return 1\n"
    assert not (attempt.path / "scratch.tmp").exists()


def test_reopen_after_a_crash(repo: Path) -> None:
    created = wt.create(repo, "INC-4", "H3")
    reopened = wt.open_existing(repo, "INC-4", "H3")
    assert reopened == created
    with pytest.raises(wt.WorktreeError, match="no worktree"):
        wt.open_existing(repo, "INC-4", "H9")


def test_existing_paths_are_not_clobbered(repo: Path) -> None:
    wt.create(repo, "INC-5", "H1")
    with pytest.raises(wt.WorktreeError, match="already exists"):
        wt.create(repo, "INC-5", "H1")


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "..", "-rf"])
def test_unsafe_names_are_refused(repo: Path, bad: str) -> None:
    with pytest.raises(wt.WorktreeError, match="unsafe"):
        wt.create(repo, bad, "H1")


def test_destructive_operations_refuse_paths_outside_wt(repo: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    outside = wt.Worktree(repo, "INC-6", "H1", elsewhere, "kintsugi/INC-6/H1", "0" * 40)
    with pytest.raises(wt.WorktreeError, match="outside"):
        wt.reset_clean(outside)
    with pytest.raises(wt.WorktreeError, match="outside"):
        wt.discard(outside)


def test_orphan_session_is_killed_before_reset(repo: Path) -> None:
    attempt = wt.create(repo, "INC-7", "H1")
    orphan = proc.start([sys.executable, "-c", "import time; time.sleep(120)"])
    wt.write_lock(attempt, orphan.pid)
    assert wt.kill_orphan(attempt) == orphan.pid
    deadline = time.monotonic() + 10
    while proc.pid_alive(orphan.pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not proc.pid_alive(orphan.pid)
    assert wt.kill_orphan(attempt) is None  # the lock is gone
