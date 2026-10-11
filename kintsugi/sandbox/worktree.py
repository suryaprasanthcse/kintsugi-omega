"""Disposable git worktrees for hypothesis attempts (spec §4.3.4, §6.6; plan §2a).

Entire 0.11.4 has no rewind. Rewinding a failed hypothesis therefore means: pin the attempt's
commit as ``refs/kintsugi/attempts/<INC>/<H>/<n>`` (so the commit, and the Entire checkpoint its
trailer names, stay reachable as evidence), then ``git worktree remove --force``.

Layout (relative to the repository): ``wt/<INC>/<H>`` is the worktree on branch
``kintsugi/<INC>/<H>``; ``wt/.meta/<INC>/<H>.json`` records its base; ``wt/.locks/<INC>/<H>.lock``
holds the PID of the session working in it, so an L2 restart can kill an orphan before reset.
``wt/`` is added to ``.git/info/exclude`` so the main worktree never sees it.

Every destructive operation (reset, clean, remove) refuses a path outside ``wt/``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from kintsugi.sandbox import proc

WT_DIR = "wt"
ATTEMPT_REF_PREFIX = "refs/kintsugi/attempts"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ZERO = "0" * 40


class WorktreeError(RuntimeError):
    pass


def _check_name(kind: str, value: str) -> str:
    if not _NAME.match(value) or ".." in value:
        raise WorktreeError(f"unsafe {kind} name: {value!r}")
    return value


def branch_name(incident: str, hypothesis: str) -> str:
    return f"kintsugi/{_check_name('incident', incident)}/{_check_name('hypothesis', hypothesis)}"


def attempt_ref(incident: str, hypothesis: str, n: int) -> str:
    if n < 1:
        raise WorktreeError("attempt numbers start at 1")
    inc, hyp = _check_name("incident", incident), _check_name("hypothesis", hypothesis)
    return f"{ATTEMPT_REF_PREFIX}/{inc}/{hyp}/{n}"


def _git(repo: Path, *args: str, timeout_s: float = 120.0) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout.strip()


@dataclass(frozen=True)
class Worktree:
    repo: Path
    incident: str
    hypothesis: str
    path: Path
    branch: str
    base_sha: str

    @property
    def root(self) -> Path:
        return self.repo / WT_DIR

    def head(self) -> str:
        return _git(self.path, "rev-parse", "HEAD")

    def has_new_commits(self) -> bool:
        return self.head() != self.base_sha


@dataclass(frozen=True)
class DiscardRecord:
    """What the rewind kept: the evidence ref (if the attempt committed) and its head."""

    pinned_ref: str | None
    head: str
    removed: bool


def _meta_path(repo: Path, incident: str, hypothesis: str) -> Path:
    return repo / WT_DIR / ".meta" / incident / f"{hypothesis}.json"


def _lock_path(repo: Path, incident: str, hypothesis: str) -> Path:
    return repo / WT_DIR / ".locks" / incident / f"{hypothesis}.lock"


def _inside_root(repo: Path, path: Path) -> bool:
    root = (repo / WT_DIR).resolve()
    return root in path.resolve().parents


def _exclude_wt(repo: Path) -> None:
    exclude = Path(_git(repo, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if f"/{WT_DIR}/" not in lines:
        exclude.write_text("\n".join([*lines, f"/{WT_DIR}/"]) + "\n", encoding="utf-8")


def create(repo: Path, incident: str, hypothesis: str, *, base: str = "HEAD") -> Worktree:
    """A fresh worktree for one hypothesis on branch ``kintsugi/<INC>/<H>``, reset to ``base``."""
    repo = Path(repo)
    branch = branch_name(incident, hypothesis)
    path = repo / WT_DIR / incident / hypothesis
    if path.exists():
        raise WorktreeError(f"{path} already exists: adopt it or discard it first")
    base_sha = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    _exclude_wt(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-B", branch, str(path), base_sha)
    meta = _meta_path(repo, incident, hypothesis)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"branch": branch, "base_sha": base_sha}), encoding="utf-8")
    return Worktree(repo, incident, hypothesis, path, branch, base_sha)


def open_existing(repo: Path, incident: str, hypothesis: str) -> Worktree:
    """Re-attach to a worktree after a crash (L2 adoption)."""
    repo = Path(repo)
    meta = _meta_path(repo, incident, hypothesis)
    path = repo / WT_DIR / incident / hypothesis
    if not meta.exists() or not path.exists():
        raise WorktreeError(f"no worktree for {incident}/{hypothesis}")
    data = json.loads(meta.read_text(encoding="utf-8"))
    return Worktree(repo, incident, hypothesis, path, data["branch"], data["base_sha"])


def attempt_count(repo: Path, incident: str, hypothesis: str) -> int:
    prefix = f"{ATTEMPT_REF_PREFIX}/{incident}/{hypothesis}/"
    refs = _git(repo, "for-each-ref", "--format=%(refname)", prefix).splitlines()
    return len([line for line in refs if line])


def pin(worktree: Worktree) -> str | None:
    """Pin the attempt's head as the next attempt ref; ``None`` if it made no commit."""
    if not worktree.has_new_commits():
        return None
    n = attempt_count(worktree.repo, worktree.incident, worktree.hypothesis) + 1
    ref = attempt_ref(worktree.incident, worktree.hypothesis, n)
    _git(worktree.repo, "update-ref", ref, worktree.head(), _ZERO)  # create-only: never overwrite
    return ref


def discard(worktree: Worktree) -> DiscardRecord:
    """Rewind: keep the evidence ref, remove the worktree and its branch."""
    if not _inside_root(worktree.repo, worktree.path):
        raise WorktreeError(f"refusing to remove {worktree.path}: outside {WT_DIR}/")
    kill_orphan(worktree)
    head = worktree.head()
    ref = pin(worktree)
    _git(worktree.repo, "worktree", "remove", "--force", str(worktree.path))
    _git(worktree.repo, "worktree", "prune")
    _git(worktree.repo, "branch", "-D", worktree.branch)
    _meta_path(worktree.repo, worktree.incident, worktree.hypothesis).unlink(missing_ok=True)
    return DiscardRecord(pinned_ref=ref, head=head, removed=not worktree.path.exists())


def reset_clean(worktree: Worktree) -> None:
    """L2 recovery for an attempt that never committed: back to its base, untracked files gone."""
    if not _inside_root(worktree.repo, worktree.path):
        raise WorktreeError(f"refusing to reset {worktree.path}: outside {WT_DIR}/")
    kill_orphan(worktree)
    _git(worktree.path, "reset", "--hard", worktree.base_sha)
    _git(worktree.path, "clean", "-fdx")


def write_lock(worktree: Worktree, pid: int) -> None:
    lock = _lock_path(worktree.repo, worktree.incident, worktree.hypothesis)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": pid}), encoding="utf-8")


def release_lock(worktree: Worktree) -> None:
    _lock_path(worktree.repo, worktree.incident, worktree.hypothesis).unlink(missing_ok=True)


def kill_orphan(worktree: Worktree) -> int | None:
    """Kill the process tree recorded in the lock, if it is still alive; return its PID."""
    lock = _lock_path(worktree.repo, worktree.incident, worktree.hypothesis)
    if not lock.exists():
        return None
    pid = int(json.loads(lock.read_text(encoding="utf-8"))["pid"])
    killed = None
    if pid != os.getpid() and proc.pid_alive(pid):
        proc.kill_tree(pid)
        killed = pid
    lock.unlink(missing_ok=True)
    return killed
