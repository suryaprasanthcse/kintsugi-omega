"""Architecture rules that keep the proofs sound.

Rule 1: only kintsugi/sandbox/proc.py may start processes. Anywhere else a child could miss
ENTIRE_TEST_TTY=0 (and hang Entire's hook), inherit secrets, or outlive its timeout.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATEWAY = REPO / "kintsugi" / "sandbox" / "proc.py"
SKIP_DIRS = {".venv", ".git", ".entire", ".claude", "node_modules", "__pycache__"}

_BANNED_MODULES = {"subprocess", "pty"}
_BANNED_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "execv"),
    ("os", "execve"),
    ("os", "execvp"),
    ("os", "execvpe"),
    ("os", "spawnv"),
    ("os", "spawnve"),
    ("os", "spawnl"),
    ("os", "posix_spawn"),
    ("os", "posix_spawnp"),
    ("os", "startfile"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
}


def process_spawning_sites(source: str) -> list[str]:
    """Return a description of every process-spawning construct in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BANNED_MODULES:
                    found.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _BANNED_MODULES:
                found.append(f"line {node.lineno}: from {node.module} import …")
            for alias in node.names:
                if (node.module, alias.name) in _BANNED_CALLS:
                    found.append(f"line {node.lineno}: from {node.module} import {alias.name}")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (node.value.id, node.attr) in _BANNED_CALLS
        ):
            found.append(f"line {node.lineno}: {node.value.id}.{node.attr}")
    return found


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO.rglob("*.py"):
        if SKIP_DIRS.intersection(path.relative_to(REPO).parts):
            continue
        files.append(path)
    return files


def test_only_the_gateway_spawns_processes() -> None:
    violations = {
        str(path.relative_to(REPO)): sites
        for path in _python_files()
        if path != GATEWAY and (sites := process_spawning_sites(path.read_text(encoding="utf-8")))
    }
    assert violations == {}, f"process spawning outside kintsugi/sandbox/proc.py: {violations}"


def test_the_scan_covers_the_package_and_tests() -> None:
    names = {path.relative_to(REPO).as_posix() for path in _python_files()}
    assert "kintsugi/sandbox/proc.py" in names
    assert "tests/test_architecture.py" in names


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import os\nos.system('ls')",
        "import os\nos.popen('ls')",
        "from os import system",
        "import asyncio\nasyncio.create_subprocess_exec('ls')",
        "import pty",
    ],
)
def test_the_scan_is_not_vacuous(source: str) -> None:
    assert process_spawning_sites(source), f"missed: {source!r}"


def test_harmless_code_is_not_flagged() -> None:
    assert process_spawning_sites("import os\nos.environ.get('PATH')\nx = 'subprocess'") == []


def test_ruff_bans_subprocess_outside_the_gateway() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]
    assert "TID" in lint["select"]
    assert "subprocess" in lint["flake8-tidy-imports"]["banned-api"]
    assert lint["per-file-ignores"] == {"kintsugi/sandbox/proc.py": ["TID251"]}
