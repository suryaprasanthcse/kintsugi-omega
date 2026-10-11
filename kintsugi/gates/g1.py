"""G1, build and static checks (spec §5.11): install, lint and type-check must exit 0."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from kintsugi.gates.common import GateResult, failed, passed
from kintsugi.gates.target import Target
from kintsugi.sandbox import proc


def commands(target: Target) -> list[list[str]]:
    found = [shlex.split(command) for command in target.install]
    if target.lint:
        found.append([sys.executable, "-m", "ruff", "check", "--no-cache", *target.lint])
    if target.typecheck:  # against Omega's interpreter, where the host's runtime is installed
        found.append([sys.executable, "-m", "pyright", "--pythonpath", sys.executable,
                      *target.typecheck])  # fmt: skip
    return found


def build_static(target: Target, checkout: Path, *, timeout_s: float = 900.0) -> GateResult:
    ran = []
    for argv in commands(target):
        result = proc.run(argv, cwd=checkout, timeout_s=timeout_s,
                          extra_env=target.code_env(checkout))  # fmt: skip
        name = " ".join(Path(argv[0]).name if i == 0 else a for i, a in enumerate(argv[:4]))
        ran.append({"command": name, "exit": result.returncode})
        if not result.ok:
            tail = (result.stdout + result.stderr).strip()[-1500:]
            return failed("G1", f"`{name}` exited {result.returncode}", commands=ran, output=tail)
    return passed("G1", f"{len(ran)} commands exited 0", commands=ran)
