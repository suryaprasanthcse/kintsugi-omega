"""Clean checkouts for the gates: a detached worktree at exactly one commit (spec §5.11).

The Auditor never judges the directory a Surgeon worked in (it may hold untracked files, and
it is the Surgeon's, not the commit's). Every gate runs on ``clean_checkout(repo, sha)``: a
fresh detached worktree outside the repository, removed afterwards. ``merged_checkout`` is the
L4 variant: the candidate merged onto a moved main, built in a scratch worktree so the attested
commit itself is never rewritten (amendment A-49).
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from kintsugi.sandbox import proc

SCRATCH_IDENTITY = ("-c", "user.name=Kintsugi Gate", "-c", "user.email=gate@kintsugi.invalid")


class MergeConflict(RuntimeError):
    def __init__(self, paths: list[str]) -> None:
        super().__init__(f"merge conflict in {paths}")
        self.paths = paths


def _git(repo: Path, *args: str, timeout_s: float = 120.0) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout


def rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


@contextlib.contextmanager
def clean_checkout(repo: Path, sha: str, *, root: Path | None = None) -> Iterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="kintsugi-gate-", dir=root))
    path = base / sha[:12]
    _git(repo, "worktree", "add", "--detach", "--force", "-q", str(path), sha)
    try:
        yield path
    finally:
        proc.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
                 timeout_s=120)  # fmt: skip
        proc.run(["git", "-C", str(repo), "worktree", "prune"], timeout_s=60)
        shutil.rmtree(base, ignore_errors=True)


@contextlib.contextmanager
def merged_checkout(repo: Path, head: str, onto: str) -> Iterator[Path]:
    """``head`` merged onto ``onto`` in a scratch worktree; :class:`MergeConflict` if it can't."""
    with clean_checkout(repo, onto) as path:
        merge = proc.run(["git", "-C", str(path), *SCRATCH_IDENTITY, "merge", "--no-ff",
                          "--no-edit", "-q", head], timeout_s=120)  # fmt: skip
        if not merge.ok:
            conflicted = _git(path, "diff", "--name-only", "--diff-filter=U").split()
            proc.run(["git", "-C", str(path), "merge", "--abort"], timeout_s=60)
            raise MergeConflict(conflicted or ["(unknown)"])
        yield path


def diff_text(repo: Path, base: str, head: str, *, context: int = 3) -> str:
    return _git(repo, "diff", f"-U{context}", f"{base}..{head}")


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", "-z", f"{base}..{head}")
    return [p for p in out.split("\0") if p]


def changed_lines(repo: Path, base: str, head: str) -> int:
    total = 0
    for line in _git(repo, "diff", "--numstat", f"{base}..{head}").splitlines():
        added, deleted, _ = line.split("\t", 2)
        total += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    return total
