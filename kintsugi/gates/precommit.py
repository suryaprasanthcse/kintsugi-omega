"""The protected-path pre-commit hook (spec §5.11: "enforced twice", the worktree half).

``install(repo)`` writes ``pre-commit`` into the repository's common hooks directory, so it runs
in every linked worktree (spike N4: worktrees share the common hooks Entire installed into). It
guards only Surgeon branches (``kintsugi/<INC>/<H>``): any staged path matching Omega's
protected-path policy (scope ``surgeon``) aborts the commit. The policy is Omega's own file,
never the target repository's copy, so a branch cannot loosen the rule it is judged by. An
existing ``pre-commit`` is kept as ``pre-commit.local`` and chained.

The Auditor re-checks the same policy on the committed diff (``kintsugi.gates.antigaming``), so
a commit that bypassed this hook (``--no-verify``) still fails its gates.

    python -m kintsugi.gates.precommit install --repo <path>
    python -m kintsugi.gates.precommit check            # what the hook runs
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from kintsugi.gates.protected_paths import load_policy
from kintsugi.sandbox import proc

MARKER = "# kintsugi-omega protected-path guard"
GUARDED_PREFIX = "kintsugi/"
CHAINED = "pre-commit.local"


def _git(cwd: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(cwd), *args], timeout_s=60, check=True).stdout.strip()


def hooks_dir(repo: Path) -> Path:
    configured = proc.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
                          timeout_s=30).stdout.strip()  # fmt: skip
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else Path(repo) / path
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else Path(repo) / common) / "hooks"


def script(python: str) -> str:
    # -I: the guard is imported from Omega's installation, never from the worktree being checked
    chained = f'"$(dirname "$0")/{CHAINED}"'
    return (f"#!/bin/sh\n{MARKER}\n"
            f'"{Path(python).as_posix()}" -I -m kintsugi.gates.precommit check || exit 1\n'
            f'if [ -x {chained} ]; then exec {chained} "$@"; fi\n'
            "exit 0\n")  # fmt: skip


def install(repo: Path, *, python: str = sys.executable) -> Path:
    folder = hooks_dir(Path(repo))
    folder.mkdir(parents=True, exist_ok=True)
    hook = folder / "pre-commit"
    if hook.exists() and MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
        hook.replace(folder / CHAINED)
    hook.write_text(script(python), encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return hook


def installed(repo: Path) -> bool:
    hook = hooks_dir(Path(repo)) / "pre-commit"
    return hook.is_file() and MARKER in hook.read_text(encoding="utf-8", errors="replace")


def check(cwd: Path) -> list[str]:
    """Problems with the staged change in ``cwd`` (empty: the commit may proceed)."""
    branch = proc.run(["git", "-C", str(cwd), "symbolic-ref", "-q", "--short", "HEAD"],
                      timeout_s=30).stdout.strip()  # fmt: skip
    if not branch.startswith(GUARDED_PREFIX):
        return []
    staged = [p for p in _git(cwd, "diff", "--cached", "--name-only", "-z").split("\0") if p]
    violations = load_policy().violations(staged, ["surgeon"])
    return [f"{v.path} matches protected pattern {v.pattern}" for v in violations]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Omega's protected-path pre-commit guard.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    inst = sub.add_parser("install")
    inst.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    if args.command == "install":
        print(f"installed {install(args.repo)}")
        return 0
    problems = check(Path.cwd())
    for problem in problems:
        print(f"kintsugi pre-commit: refused: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
