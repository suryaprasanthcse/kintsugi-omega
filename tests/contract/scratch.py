"""Scratch git repositories for contract tests and spikes (never pushed anywhere)."""

from __future__ import annotations

import shutil
from pathlib import Path

from kintsugi.sandbox import proc

IDENTITY = {"user.name": "Surya Prasanth R", "user.email": "suryaprasanthcse@gmail.com"}


def git(repo: Path, *args: str, timeout_s: float = 120) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout


def make_repo(destination: Path, source: Path | None = None) -> Path:
    """A fresh repository on ``main`` with one commit, optionally seeded from ``source``."""
    destination.mkdir(parents=True, exist_ok=True)
    if source is not None:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        (destination / "README.md").write_text("# scratch\n", encoding="utf-8", newline="\n")
    git(destination, "init", "-q", "-b", "main")
    for key, value in IDENTITY.items():
        git(destination, "config", key, value)
    git(destination, "config", "core.autocrlf", "false")
    git(destination, "add", "-A")
    git(destination, "commit", "-q", "-m", "scratch: initial")
    return destination


def enable_entire(repo: Path) -> None:
    """Install Entire's Claude Code and git hooks non-interactively, then commit them."""
    proc.run(
        ["entire", "enable", "--agent", "claude-code", "--telemetry=false", "--skip-push-sessions"],
        cwd=repo,
        timeout_s=180,
        check=True,
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "scratch: enable entire")
