"""Secret redaction for public transcripts, and the honeytoken blindness check (audit C6).

Checkpoint transcripts are pushed with the code (docs/EXECUTION_PLAN.md §2e-4), so two layers
keep secrets out of them:

1. **Entire's own redaction** runs before a transcript is stored. ``ENTIRE_RULES`` is written to
   ``.entire/settings.json`` as ``redaction.custom_redactions`` (a ``name → RE2 regex`` map; the
   schema was verified against Entire 0.11.4, which rejects list forms).
2. **A pre-push scan** of every checkpoint tree with the same rules (:func:`assert_pushable`):
   if Entire ever missed one, nothing is pushed.

``kintsugi_redaction_probe`` is a permanent positive control: planting
``kintsugi-redaction-probe-NNNNNN`` in a session and finding it redacted in the stored transcript
proves the redaction layer is live, which is what makes "zero hits" meaningful.

C6 (spec §7.2): a sealed sabotage's honeytoken must have zero hits in every checkpoint
transcript. :func:`honeytoken_hits` greps all ``refs/entire/checkpoints/*`` trees for the exact
string. Because honeytokens are passphrases (spike N1), Entire's redaction leaves them intact, so
a leak is visible rather than silently blanked.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from kintsugi.sandbox import proc

PathLike = str | os.PathLike[str]
CHECKPOINT_REFS = "refs/entire/checkpoints/"

# Valid in both Python `re` and Go RE2.
ENTIRE_RULES: Mapping[str, str] = {
    "anthropic_api_key": r"sk-ant-[A-Za-z0-9_\-]{20,}",
    "neatlogs_api_key": (
        r"(?i)neatlogs[_-]?(?:api[_-]?key|service[_-]?token|oauth[_-]?token|webhook[_-]?secret)"
        r"[\"' ]*[:=][\"' ]*[A-Za-z0-9_.\-]{16,}"
    ),
    "neatlogs_access_token": r"nl(?:sa|oa)_[A-Za-z0-9_\-]{16,}",
    "bearer_token": r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]{20,}",
    "github_token": r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})",
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "age_secret_key": r"AGE-SECRET-KEY-1[0-9A-Z]{58}",
    "private_key_block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "kintsugi_redaction_probe": r"kintsugi-redaction-probe-[0-9]{6}",
}
PROBE_RULE = "kintsugi_redaction_probe"
_COMPILED = {name: re.compile(pattern) for name, pattern in ENTIRE_RULES.items()}


class SecretsFound(RuntimeError):
    def __init__(self, findings: list[Finding]) -> None:
        super().__init__(f"{len(findings)} secret-like strings in checkpoint trees; refusing push")
        self.findings = findings


@dataclass(frozen=True)
class Finding:
    rule: str
    where: str
    line: int
    excerpt: str  # masked: never echoes the secret itself


def _mask(match: str) -> str:
    return match[:6] + "…" + f"({len(match)} chars)"


def scan_text(text: str, *, where: str = "<text>", include_probe: bool = False) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _COMPILED.items():
            if name == PROBE_RULE and not include_probe:
                continue
            for match in pattern.finditer(line):
                findings.append(Finding(name, where, number, _mask(match.group(0))))
    return findings


def redact(text: str) -> str:
    for name, pattern in _COMPILED.items():
        text = pattern.sub(f"REDACTED[{name}]", text)
    return text


def entire_settings_with_rules(settings: Mapping[str, object]) -> dict[str, object]:
    """``settings`` with ``redaction.custom_redactions`` holding every rule (other keys kept)."""
    merged = json.loads(json.dumps(settings))
    redaction = merged.setdefault("redaction", {})
    if not isinstance(redaction, dict):
        raise ValueError("redaction must be an object")
    custom = redaction.setdefault("custom_redactions", {})
    if not isinstance(custom, dict):
        raise ValueError("redaction.custom_redactions must be a name → regex map")
    custom.update(ENTIRE_RULES)
    return merged


def install_entire_rules(repo: PathLike) -> Path:
    path = Path(repo) / ".entire" / "settings.json"
    current = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(entire_settings_with_rules(current), indent=2) + "\n",
                    encoding="utf-8", newline="\n")  # fmt: skip
    return path


def _git_out(repo: PathLike, *args: str) -> str:
    return proc.run(["git", "-C", os.fspath(repo), *args], timeout_s=120, check=True).stdout


def checkpoint_refs(repo: PathLike) -> list[str]:
    out = _git_out(repo, "for-each-ref", "--format=%(refname)", CHECKPOINT_REFS)
    return [line for line in out.splitlines() if line]


def scan_refs(repo: PathLike, refs: Iterable[str] | None = None) -> list[Finding]:
    """Every blob of every checkpoint tree, scanned with the same rules Entire applies."""
    findings: list[Finding] = []
    seen: set[str] = set()
    for ref in checkpoint_refs(repo) if refs is None else list(refs):
        for line in _git_out(repo, "ls-tree", "-r", ref).splitlines():
            meta, path = line.split("\t", 1)
            sha = meta.split()[2]
            if sha in seen:
                continue
            seen.add(sha)
            data = proc.run_binary(["git", "-C", os.fspath(repo), "cat-file", "-p", sha])
            findings += scan_text(data.decode("utf-8", errors="replace"), where=f"{ref}:{path}")
    return findings


def assert_pushable(repo: PathLike, refs: Iterable[str] | None = None) -> None:
    findings = scan_refs(repo, refs)
    if findings:
        raise SecretsFound(findings)


def honeytoken_hits(
    repo: PathLike, token: str, refs: Iterable[str] | None = None
) -> list[tuple[str, str]]:
    """(ref, path) pairs whose stored content contains ``token`` verbatim (audit C6)."""
    targets = checkpoint_refs(repo) if refs is None else list(refs)
    if not targets:
        return []
    result = proc.run(
        ["git", "-C", os.fspath(repo), "grep", "-F", "-l", "-e", token, *targets], timeout_s=300
    )
    if result.returncode == 1:  # git grep: no match
        return []
    if not result.ok:
        raise RuntimeError(f"git grep failed: {result.stderr.strip()[-500:]}")
    hits = []
    for line in result.stdout.splitlines():
        ref, _, path = line.partition(":")
        hits.append((ref, path))
    return hits
