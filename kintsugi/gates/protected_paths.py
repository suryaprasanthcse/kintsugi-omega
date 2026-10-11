"""Protected-path policy (``.kintsugi/protected_paths.yaml``; spec §5.11, §6.9, audit C14).

The same matcher backs the worktree pre-commit hook, the Auditor's diff check and audit check
C14, so the three can never disagree about what is protected.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

import yaml

DEFAULT_POLICY = Path(__file__).resolve().parents[2] / ".kintsugi" / "protected_paths.yaml"
ESCAPE = "<escapes-worktree>"


@dataclass(frozen=True)
class Violation:
    path: str
    scope: str
    pattern: str


@dataclass(frozen=True)
class Policy:
    diff_budget_lines: int
    scopes: Mapping[str, tuple[str, ...]]

    def violations(self, changed_paths: Iterable[str], scopes: Iterable[str]) -> list[Violation]:
        selected = list(scopes)
        unknown = [scope for scope in selected if scope not in self.scopes]
        if unknown:
            raise KeyError(f"unknown protected-path scopes: {unknown}")
        found: list[Violation] = []
        for raw in changed_paths:
            path = normalise(raw)
            for scope in selected:
                if path is None:
                    found.append(Violation(raw, scope, ESCAPE))
                    continue
                for pattern in self.scopes[scope]:
                    if glob_regex(pattern).fullmatch(path):
                        found.append(Violation(path, scope, pattern))
                        break
        return found

    def over_budget(self, changed_lines: int) -> bool:
        return changed_lines > self.diff_budget_lines


def load_policy(path: str | os.PathLike[str] = DEFAULT_POLICY) -> Policy:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("unsupported protected-path policy version")
    scopes = {name: tuple(scope["patterns"]) for name, scope in data["scopes"].items()}
    return Policy(diff_budget_lines=int(data["diff_budget_lines"]), scopes=scopes)


def normalise(path: str) -> str | None:
    """Repository-relative POSIX form, or ``None`` if the path escapes the worktree."""
    text = path.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts: list[str] = []
    for part in PurePosixPath(text).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts) if parts else None


@cache
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a POSIX glob: ``*`` and ``?`` stay within a segment, ``**`` spans segments."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))
